"""The production seed (PRODUCTION_SETUP.md): Services, UserLevels and SiteConfiguration only."""

import json
import os
import tempfile
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from accounts.models import UserLevel
from common.models import SiteConfiguration
from orders.testing import make_admin_wallet
from services_catalog.models import Service

SEED_MODELS = ["services_catalog.Service", "accounts.UserLevel", "common.SiteConfiguration"]


class SeedConfigRoundTripTests(TestCase):
    def test_dumpdata_and_loaddata_carry_exactly_the_configuration(self):
        UserLevel.objects.create(code="gold", name="Gold", minimum_approved_tasks=50, display_order=3, strike_limit=4)
        Service.objects.create(name="Followers", price_per_1000=Decimal("120.5000"), eligible_level="gold",
                               user_reward_percentage=Decimal("30.00"), has_fulfillment_cost=True,
                               fulfillment_cost_per_1000=Decimal("9.0000"), target_type="profile")
        config = SiteConfiguration.load()
        config.claim_timeout_minutes = 45
        config.save()
        _client, key = make_admin_wallet()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seed_config.json")
            call_command("dumpdata", *SEED_MODELS, indent=2, output=path, verbosity=0)
            with open(path) as handle:
                exported = handle.read()
            self.assertEqual({row["model"] for row in json.loads(exported)},
                             {"services_catalog.service", "accounts.userlevel", "common.siteconfiguration"})
            self.assertNotIn(key, exported)
            self.assertNotIn("key_hash", exported)

            Service.objects.all().delete()
            UserLevel.objects.all().delete()
            SiteConfiguration.objects.all().delete()
            call_command("loaddata", path, verbosity=0)

        service = Service.objects.get()
        self.assertEqual((service.name, service.business_share_percentage, service.target_type, service.fulfillment_cost_per_1000),
                         ("Followers", Decimal("70.00"), "profile", Decimal("9.0000")))
        self.assertEqual(UserLevel.objects.get().strike_limit, 4)
        self.assertEqual(SiteConfiguration.load().claim_timeout_minutes, 45)
