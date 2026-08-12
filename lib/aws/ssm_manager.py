"""
lib/aws/ssm_manager.py

Thin, testable wrapper around boto3 SSM. This holds only generic
send-command / wait-for-command / instance-status plumbing that any
future SSM-based automation (not just backup-restore) can reuse.

Deliberately has no argparse and never calls sys.exit() — it raises
SSMError so pytest can assert on failures instead of the whole test
process dying.
"""

import time
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    CYAN = "\033[0;36m"
    RESET = "\033[0m"


def log_info(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Colors.GREEN}[INFO]{Colors.RESET}    {ts} | {msg}", flush=True)


def log_warn(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET}    {ts} | {msg}", flush=True)


def log_error(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Colors.RED}[ERROR]{Colors.RESET}   {ts} | {msg}", flush=True)


def log_poll(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Colors.CYAN}[POLLING]{Colors.RESET} {ts} | {msg}", flush=True)


class SSMError(Exception):
    """Raised for unrecoverable SSM failures (bad creds, instance offline, etc.)."""


class SSMManager:
    def __init__(self, region, profile=None, access_key=None, secret_key=None):
        try:
            if profile:
                session = boto3.Session(profile_name=profile, region_name=region)
                self.client = session.client("ssm")
                frozen = session.get_credentials().get_frozen_credentials()
                self.access_key = frozen.access_key
                self.secret_key = frozen.secret_key
            else:
                self.client = boto3.client(
                    "ssm",
                    region_name=region,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                )
                self.access_key = access_key
                self.secret_key = secret_key
            log_info(f"SSM client created for region: {region}")
        except NoCredentialsError:
            raise SSMError("AWS credentials not found.")

    def check_instance_online(self, instance_id):
        log_info(f"Checking SSM status for instance: {instance_id}")
        try:
            response = self.client.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
        except ClientError as e:
            raise SSMError(f"AWS ClientError: {e}")

        instances = response.get("InstanceInformationList", [])
        if not instances:
            raise SSMError(f"Instance '{instance_id}' not found in SSM.")

        status = instances[0].get("PingStatus", "Unknown")
        if status != "Online":
            raise SSMError(f"Instance SSM status: {status}.")
        log_info("Instance is Online ✅")

    def send_command(self, instance_id, commands, timeout=30, comment=""):
        try:
            response = self.client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": commands},
                TimeoutSeconds=timeout,
                Comment=comment,
            )
            return response["Command"]["CommandId"]
        except ClientError as e:
            log_error(f"send_command failed: {e}")
            return None

    def wait_for_command(self, instance_id, command_id, timeout=180):
        elapsed = 0
        while elapsed < timeout:
            try:
                response = self.client.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
                status = response.get("Status", "Pending")
                if status in ("Success", "Failed", "TimedOut", "Cancelled"):
                    return {
                        "status": status,
                        "stdout": response.get("StandardOutputContent", ""),
                        "stderr": response.get("StandardErrorContent", ""),
                    }
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvocationDoesNotExist":
                    log_error(f"Error fetching command result: {e}")
            time.sleep(3)
            elapsed += 3
        return {"status": "TimedOut", "stdout": "", "stderr": ""}
