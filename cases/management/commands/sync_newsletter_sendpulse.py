"""Sync local newsletter subscriptions to SendPulse."""

from django.core.management.base import BaseCommand

from cases.models import NewsletterSubscription, NewsletterSubscriptionStatus
from cases.services.sendpulse import (
    SYNC_STATUS_FAILED,
    SYNC_STATUS_SUBSCRIBED,
    sync_subscription_to_sendpulse,
)


class Command(BaseCommand):
    help = "Sync newsletter subscriptions from Jawafdehi to SendPulse."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Sync every newsletter record, including already-synced records.",
        )
        parser.add_argument(
            "--failed",
            action="store_true",
            help="Sync only records whose last SendPulse attempt failed.",
        )
        parser.add_argument(
            "--unsubscribed",
            action="store_true",
            help="Include unsubscribed records so SendPulse receives unsubscribe state.",
        )

    def handle(self, *args, **options):
        queryset = NewsletterSubscription.objects.order_by("id")
        if not options["unsubscribed"]:
            queryset = queryset.filter(status=NewsletterSubscriptionStatus.SUBSCRIBED)
        if options["failed"]:
            queryset = queryset.filter(sendpulse_sync_status=SYNC_STATUS_FAILED)
        elif not options["all"]:
            queryset = queryset.exclude(sendpulse_sync_status=SYNC_STATUS_SUBSCRIBED)

        count = 0
        for subscription in queryset.iterator():
            sync_subscription_to_sendpulse(subscription)
            count += 1

        self.stdout.write(self.style.SUCCESS(f"Synced {count} subscription(s)."))
