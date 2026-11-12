from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import User
from . import system_health


class CpuSampleTests(SimpleTestCase):
    def setUp(self):
        self.previous = system_health._last_cpu_sample
        system_health._last_cpu_sample = None
        self.addCleanup(setattr, system_health, '_last_cpu_sample', self.previous)

    def test_cold_sample_returns_counters_then_real_percent_without_sleep(self):
        values = {'/sys/fs/cgroup/cpu.stat': 'usage_usec 1000000\n',
            '/sys/fs/cgroup/cpu.max': '200000 100000',
            '/sys/fs/cgroup/memory.current': '1048576', '/sys/fs/cgroup/memory.max': '2097152'}
        with patch.object(system_health, '_read_cgroup_value', side_effect=lambda *paths: values.get(paths[0])), patch.object(system_health.time, 'monotonic', side_effect=[100.0, 101.0]), patch.object(system_health.time, 'sleep') as sleep:
            first = system_health.get_web_health()
            self.assertIsNone(first['cpu_percent'])
            self.assertEqual(first['cpu_sample'], {'usage_usec': 1000000, 'sampled_at': 100.0, 'available_cpus': 2})
            values['/sys/fs/cgroup/cpu.stat'] = 'usage_usec 2000000\n'
            second = system_health.get_web_health()
        self.assertEqual(second['cpu_percent'], 50)
        self.assertEqual(second['memory_percent'], 50)
        sleep.assert_not_called()

    def test_unavailable_counters_are_explicitly_absent(self):
        with patch.object(system_health, '_read_cgroup_value', return_value=None):
            health = system_health.get_web_health()
        self.assertIsNone(health['cpu_percent'])
        self.assertIsNone(health['cpu_sample'])

    def test_counter_reset_does_not_report_fake_zero(self):
        self.assertIsNone(system_health._container_cpu_percent((1000000, 100, 1)))
        self.assertIsNone(system_health._container_cpu_percent((500000, 101, 1)))


class WebHealthEndpointTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='health-admin')
        self.admin.signature.name = 'signatures/test/health-admin.png'
        self.admin.save()
        self.client.force_login(self.admin)

    def test_admin_get_is_uncached_and_supplies_cross_worker_samples(self):
        sample = {'usage_usec': 1000000, 'sampled_at': 100.0, 'available_cpus': 2}
        with patch('apps.core.views.get_web_health', return_value={'cpu_percent': None, 'cpu_sample': sample, 'running': True}):
            response = self.client.get(reverse('system_web_health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cpu_sample'], sample)
        self.assertTrue(response.json()['database_ok'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])

    def test_only_admin_get_can_read_health(self):
        self.assertEqual(self.client.post(reverse('system_web_health')).status_code, 405)
        user = User.objects.create_user(username='health-requester')
        user.signature.name = 'signatures/test/health-requester.png'
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('system_web_health')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('system_web_health')).status_code, 302)

    def test_overview_has_live_health_without_changing_layout(self):
        response = self.client.get(reverse('system_overview'))
        self.assertContains(response, 'data-web-health')
        self.assertContains(response, reverse('system_web_health'))
        self.assertContains(response, 'js/web-health.js?v=20261111-01')
