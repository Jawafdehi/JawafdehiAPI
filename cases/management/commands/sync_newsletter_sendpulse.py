"""Sync local newsletter subscriptions to SendPulse."""

from django.conf import settings
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
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument(
            "--all",
            action="store_true",
            help="Sync every newsletter record, including already-synced records.",
        )
        scope.add_argument(
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
        if not settings.SENDPULSE_ENABLED:
            # Every record would otherwise be iterated and marked
            # SYNC_STATUS_DISABLED one by one — skip the wasted writes and make
            # the no-op explicit to the operator.
            self.stdout.write(
                self.style.WARNING(
                    "SendPulse sync is disabled (SENDPULSE_ENABLED=False). "
                    "Nothing to do."
                )
            )
            return

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
