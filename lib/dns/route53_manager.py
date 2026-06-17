"""
lib/dns/route53_manager.py

Manages Route53 DNS records using boto3.
Uses aws_profile from dev.yaml dns.aws_profile — same profile used by AWS CLI.

Flow:
    create_record(ip)  → *.shc-42.dev.rafay-edge.net → <VM IP>
    delete_record()    → removes the record on teardown
"""

import boto3
from typing import Optional


class Route53Manager:
    """
    Creates and deletes a wildcard A record for a controller run.

    Record pattern: *.{display_name}.{base_domain} → <VM public IP>
    Example:        *.shc-42.dev.rafay-edge.net     → 137.131.33.215
    """

    def __init__(self, dns_cfg: dict, display_name: str):
        """
        Args:
            dns_cfg:      The dns: section from dev.yaml
            display_name: e.g. "shc-42"
        """
        self.hosted_zone_id = dns_cfg.get("hosted_zone_id", "")
        self.base_domain    = dns_cfg.get("base_domain", "dev.rafay-edge.net")
        self.ttl            = int(dns_cfg.get("ttl", 60))
        self.aws_profile    = dns_cfg.get("aws_profile", "default")
        self.display_name   = display_name
        self.record_name    = f"*.{display_name}.{self.base_domain}"

        # Use the profile from dev.yaml — same as AWS CLI vijay-aws
        session = boto3.Session(profile_name=self.aws_profile)
        self.client = session.client("route53")

    def create_record(self, ip: str):
        """
        Create wildcard A record: *.shc-42.dev.rafay-edge.net → <ip>
        Uses UPSERT so it's safe to call even if record already exists.
        """
        print(f"[Route53Manager] Creating: {self.record_name} → {ip}")

        self.client.change_resource_record_sets(
            HostedZoneId=self.hosted_zone_id,
            ChangeBatch={
                "Comment": f"rafay-pytest-framework: {self.display_name}",
                "Changes": [{
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": self.record_name,
                        "Type": "A",
                        "TTL":  self.ttl,
                        "ResourceRecords": [{"Value": ip}],
                    }
                }]
            }
        )
        print(f"[Route53Manager] DNS record created: {self.record_name} → {ip}")

    def delete_record(self, ip: str):
        """
        Delete the wildcard A record.
        Safe to call even if record doesn't exist.
        """
        print(f"[Route53Manager] Deleting: {self.record_name}")
        try:
            self.client.change_resource_record_sets(
                HostedZoneId=self.hosted_zone_id,
                ChangeBatch={
                    "Comment": f"rafay-pytest-framework cleanup: {self.display_name}",
                    "Changes": [{
                        "Action": "DELETE",
                        "ResourceRecordSet": {
                            "Name": self.record_name,
                            "Type": "A",
                            "TTL":  self.ttl,
                            "ResourceRecords": [{"Value": ip}],
                        }
                    }]
                }
            )
            print(f"[Route53Manager] DNS record deleted: {self.record_name}")
        except self.client.exceptions.InvalidChangeBatch:
            print(f"[Route53Manager] Record not found — nothing to delete")
        except Exception as e:
            print(f"[Route53Manager] Delete warning: {e}")

    @property
    def fqdn(self) -> str:
        """The wildcard FQDN e.g. *.shc-42.dev.rafay-edge.net"""
        return self.record_name

    @property
    def star_domain(self) -> str:
        """Value for radm config.yaml star-domain field."""
        return self.record_name