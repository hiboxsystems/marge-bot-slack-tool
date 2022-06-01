#!/usr/bin/env python3

#
# Script which posts the current content of the Margebot queue to Slack
#

from datetime import datetime
import json
import logging
import os
from pathlib import Path

import requests

import marge.user
import marge.project
from marge import gitlab
from marge.merge_request import MergeRequest

PROJECT_IDS = os.getenv('MARGEBOT_PROJECT_IDS')
GITLAB_URL = os.getenv('MARGEBOT_GITLAB_URL')
GITLAB_TOKEN = os.getenv('MARGEBOT_GITLAB_TOKEN')
SLACK_CHANNEL = os.getenv('MARGEBOT_HELPER_SLACK_CHANNEL')
SLACK_WEBHOOK_URL = os.getenv('MARGEBOT_HELPER_SLACK_WEBHOOK_URL')
STATE_FILE_PATH_PREFIX = os.getenv('MARGEBOT_HELPER_STATE_FILE_PATH_PREFIX')

if PROJECT_IDS is None:
    raise Exception('The MARGEBOT_PROJECT_IDS environment variable must be set to '
                    'a valid GitLab project ID')

if GITLAB_URL is None:
    raise Exception('The MARGEBOT_GITLAB_URL environment variable must be set to '
                    'the full GitLab installation URL')

if GITLAB_TOKEN is None:
    raise Exception('The MARGEBOT_GITLAB_TOKEN environment variable must be set to '
                    'a valid Margebot GitLab authentication token')

if SLACK_CHANNEL is None:
    raise Exception('The MARGEBOT_HELPER_SLACK_CHANNEL environment variable must '
                    'be set to the Slack channel where you want the content of '
                    'the queue to be posted')

if SLACK_WEBHOOK_URL is None:
    raise Exception('The MARGEBOT_HELPER_SLACK_WEBHOOK_URL environment variable '
                    'must be set to the Slack webhook URL')

if STATE_FILE_PATH_PREFIX is None:
    raise Exception('The MARGEBOT_HELPER_STATE_FILE_PATH_PREFIX environment '
                    'variable must be set to the full path (directory + file '
                    'name prefix) to where the state files will be located. The'
                    'full path will be <prefix>-<project-id>.json.')

LOGGER = logging.getLogger(name=os.path.basename(__file__))

# Enable to log all HTTPS requests (and more)
DEBUG = os.getenv('DEBUG')

if DEBUG is None:
    LOGGER.setLevel(logging.INFO)
else:
    # Enable debug logging both for the root logger (=3rd party code like urllib3) and ourselves
    logging.getLogger().setLevel(logging.DEBUG)
    LOGGER.setLevel(logging.DEBUG)


class MargebotQueuePoster:
    project_id: int = None
    slack_channel: str = None
    slack_webhook_url: str = None
    state_file_path: str = None

    def __init__(self, project_id: int, gitlab_url: str, gitlab_token: str,
                 slack_channel: str, slack_webhook_url: str,
                 state_file_path: str):

        self.project_id = project_id
        self.slack_channel = slack_channel
        self.slack_webhook_url = slack_webhook_url
        self.state_file_path = state_file_path

        self.api = gitlab.Api(gitlab_url, gitlab_token)

    def run(self):
        try:
            project = marge.project.Project.fetch_by_id(self.project_id, api=self.api)
        except gitlab.NotFound:
            LOGGER.warning(f"Project {self.project_id} not found, ignoring")
            return

        project_name = project.name

        self_assigned_merge_requests = self.get_merge_requests_assigned_to_self()

        merge_requests_state = self.merge_requests_state_to_json_serializable_format(self_assigned_merge_requests)

        if Path(self.state_file_path).exists():
            old_state_json = Path(self.state_file_path).read_text()
            old_state = json.loads(old_state_json)

            if old_state == merge_requests_state:
                LOGGER.debug('Assigned merge requests state not modified since last run, not posting to Slack.')
                return

        # Make sure to serialize the state to disk *before* posting to slack, to avoid spamming Slack
        # infinitely in case something goes with error handling etc.
        with open(self.state_file_path, 'w') as f:
            state_json = json.dumps(merge_requests_state)
            f.write(state_json)

        if len(self_assigned_merge_requests) == 0:
            LOGGER.debug('No merge requests assigned to self, not posting to Slack')
        elif len(self_assigned_merge_requests) < 2:
            LOGGER.debug('Threshold for number of MRs assigned to self not reached, not posting to Slack')
        else:
            markdown_message = self.merge_requests_to_mrkdwn(self_assigned_merge_requests)
            self.post_to_slack(f'{project_name} pending merge requests', markdown_message)

    def get_merge_requests_assigned_to_self(self):
        user = marge.user.User.myself(self.api)

        return MergeRequest.fetch_all_open_for_user(
            self.project_id, user=user, api=self.api, merge_order='assigned_at'
        )

    # Note: `mrkdwn` is not fully Markdown-compatible. More details: https://api.slack.com/reference/surfaces/formatting#basics
    @staticmethod
    def merge_requests_to_mrkdwn(merge_requests):
        result = ''

        i = 1

        for mr in merge_requests:
            waiting_minutes = int((datetime.utcnow() - datetime.utcfromtimestamp(mr.assigned_at)).total_seconds() / 60)

            result += f'{i}. <{mr.web_url}|{mr.title}> (!{mr.iid}. Time in queue: {waiting_minutes}m)\n'
            i += 1

        return result

    @staticmethod
    def merge_requests_state_to_json_serializable_format(merge_requests):
        result = []

        for mr in merge_requests:
            result.append(dict(
                id=mr.iid,
                assigned_at=mr.assigned_at
            ))

        return result

    def post_to_slack(self, title, content):
        payload = dict(
            channel=self.slack_channel,
            username=title,
            icon_emoji=":marge:",
            text=content
        )

        result = requests.post(self.slack_webhook_url, json=payload, headers={'Content-Type': 'application/json'})

        if result.status_code != 200:
            LOGGER.error(f'Error response from server: {result.status_code}. More details may be available in logs.')

            from pprint import pprint
            pprint(vars(result))

            raise Exception(f'Error response from server: {result.status_code}. More details may be available in logs.')


# Program entry point
project_ids = PROJECT_IDS.split(',')

for project_id in project_ids:
    state_file_path = f'{STATE_FILE_PATH_PREFIX}-{project_id}.json'

    margebot_queue_poster = MargebotQueuePoster(project_id=int(project_id),
                                                gitlab_url=GITLAB_URL,
                                                gitlab_token=GITLAB_TOKEN,
                                                slack_channel=SLACK_CHANNEL,
                                                slack_webhook_url=SLACK_WEBHOOK_URL,
                                                state_file_path=state_file_path)

    margebot_queue_poster.run()
