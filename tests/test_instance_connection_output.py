"""Read-only instance listing; no real network probes or service mutations."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server_instances as instances


CONNECTED = {"status": "connected", "ipv4": "100.100.10.20"}
URLS = ["http://127.0.0.1:7851", "http://100.100.10.20:7851", "http://192.168.1.20:7851"]


class ConnectionChoicesTests(unittest.TestCase):
    def choices(self, bind="0.0.0.0", network=None, urls=None):
        return instances.connection_choices(bind, 7851, URLS if urls is None else urls, CONNECTED if network is None else network)

    def test_connected_tailscale_is_separate_from_lan_and_loopback(self):
        choices = self.choices()
        self.assertEqual(choices["tailscale"], URLS[1])
        self.assertEqual(choices["local"], [URLS[0]])
        self.assertEqual(choices["lan"], [URLS[2]])
        self.assertEqual(choices["other"], [])

    def test_tail_address_comes_from_status_even_if_not_in_interface_discovery(self):
        self.assertEqual(self.choices(urls=[URLS[0], URLS[2]])["tailscale"], URLS[1])

    def test_localhost_and_loopback_bindings_never_offer_tailscale(self):
        for bind, url in (("localhost", "http://localhost:7851"), ("127.0.0.1", URLS[0]), ("::1", "http://[::1]:7851")):
            with self.subTest(bind=bind):
                choices = self.choices(bind, urls=[url + " (This machine only)"])
                self.assertEqual(choices["tailscale"], "")
                self.assertIn("bound to this machine only", choices["tailscale_note"])
                self.assertEqual(choices["local"], [url])
                self.assertEqual(choices["lan"], [])

    def test_lan_and_ipv6_bindings_do_not_promise_ipv4_tailscale_access(self):
        for bind, url in (("192.168.1.20", URLS[2]), ("::", "http://[::1]:7851"), ("server.local", "http://server.local:7851")):
            with self.subTest(bind=bind):
                choices = self.choices(bind, urls=[url])
                self.assertEqual(choices["tailscale"], "")
                self.assertIn("Not verified", choices["tailscale_note"])

    def test_exact_tailscale_binding_does_not_invent_loopback_or_lan_routes(self):
        choices = self.choices(CONNECTED["ipv4"], urls=[URLS[1]])
        self.assertEqual(choices["tailscale"], URLS[1])
        self.assertEqual(choices["local"], [])
        self.assertEqual(choices["lan"], [])

    def test_disconnected_or_unknown_tailscale_never_trusts_a_100_range_candidate(self):
        for state, note in (("disconnected", "disconnected"), ("not-installed", "not found"), ("unavailable", "could not read")):
            with self.subTest(state=state):
                choices = self.choices(network={"status": state, "ipv4": ""})
                self.assertEqual(choices["tailscale"], "")
                self.assertIn(note, choices["tailscale_note"])
                self.assertEqual(choices["other"], [URLS[1]])
                self.assertEqual(choices["lan"], [URLS[2]])

    def test_no_ipv4_does_not_generate_an_empty_host_url(self):
        choices = self.choices(network={"status": "connected", "ipv4": ""})
        self.assertEqual(choices["tailscale"], "")
        self.assertIn("no usable IPv4", choices["tailscale_note"])

    def test_only_private_ipv4_candidates_are_labelled_lan(self):
        choices = self.choices(urls=["http://10.1.2.3:7851", "http://172.16.1.2:7851", URLS[2], "http://8.8.8.8:7851", "http://[fd7a:115c:a1e0::1]:7851", "http://[fd00::1]:7851"])
        self.assertEqual(choices["lan"], ["http://10.1.2.3:7851", "http://172.16.1.2:7851", URLS[2]])
        self.assertEqual(len(choices["other"]), 3)

    def test_stale_custom_tail_address_is_not_relabelled_as_lan(self):
        choices = self.choices(network={"status": "disconnected", "ipv4": "192.168.1.20"})
        self.assertEqual(choices["tailscale"], "")
        self.assertEqual(choices["lan"], [])
        self.assertIn(URLS[2], choices["other"])

    def test_nonconnectable_candidates_and_duplicates_are_omitted(self):
        choices = self.choices(urls=[URLS[0], URLS[0], "http://0.0.0.0:7851", "http://224.0.0.1:7851", "http://169.254.1.1:7851"])
        self.assertEqual(choices["local"], [URLS[0]])
        self.assertEqual(choices["other"], [])


class InstanceListOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agents-network-output-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir(mode=0o700)
        self.registry = instances.Registry(self.home)
        self.add_instance("default", 7850)
        self.add_instance("work", 7851)

    def add_instance(self, name, port, bind="0.0.0.0"):
        instance = instances.Instance(name, self.home)
        instance.config.mkdir(parents=True, exist_ok=True)
        (instance.config / "env").write_text(f"AGENTSDOCK_AGENT_PORT={port}\nAGENTSDOCK_AGENT_BIND={bind}\nAGENTSDOCK_AGENT_TOKEN=synthetic-secret\n")
        return instance

    def run_list(self, network=None, service="running", args=None, terminal=False):
        output = io.StringIO()
        with patch.object(output, "isatty", return_value=terminal), patch.object(instances, "Registry", return_value=self.registry), patch.object(instances, "service_status", return_value=service), patch.object(instances, "tailscale_status", return_value=CONNECTED if network is None else network) as probe, patch.object(instances, "candidate_addresses", side_effect=lambda bind, port: [url.replace(":7851", f":{port}") for url in URLS]), patch.object(instances, "run") as run, contextlib.redirect_stdout(output):
            code = instances.main(["list"] if args is None else args)
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertFalse(self.registry.root.exists())
        return output.getvalue(), probe

    def test_terminal_colors_each_name_blue_without_changing_alignment_or_urls(self):
        with patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            colored, _ = self.run_list(terminal=True)
            plain, _ = self.run_list()
        for name in ("default", "work"):
            self.assertIn(f"\033[34m{name:<20}\033[0m running", colored)
        self.assertEqual(colored.count("\033[34m"), 2)
        self.assertEqual(colored.replace("\033[34m", "").replace("\033[0m", ""), plain)

    def test_redirected_output_has_no_color_codes(self):
        with patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            output, _ = self.run_list()
        self.assertNotIn("\033[", output)

    def test_no_color_keeps_terminal_names_plain(self):
        for value in ("", "1"):
            with self.subTest(value=value), patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": value}, clear=True):
                output, _ = self.run_list(terminal=True)
            self.assertNotIn("\033[", output)

    def test_dumb_terminal_keeps_names_plain(self):
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            output, _ = self.run_list(terminal=True)
        self.assertNotIn("\033[", output)

    def test_list_labels_each_route_and_probes_tailscale_once_for_all_instances(self):
        output, probe = self.run_list()
        probe.assert_called_once_with()
        self.assertIn("NAME                 STATUS     PORT\n", output)
        self.assertIn("Tailscale / other networks: http://100.100.10.20:7850 (recommended)", output)
        self.assertIn("Tailscale / other networks: http://100.100.10.20:7851 (recommended)", output)
        self.assertIn("Same Wi-Fi / LAN:          http://192.168.1.20:7851", output)
        self.assertIn("This machine only:         http://127.0.0.1:7851", output)
        self.assertNotIn("Tailscale is connected on this machine", output)
        self.assertNotIn("Each instance uses one port", output)
        self.assertNotIn("connect both devices to the same Tailscale network", output)
        self.assertNotIn("Local checks cannot verify the other device", output)
        self.assertNotIn("access rules and firewalls", output)

    def test_unverified_tailscale_prints_reason_not_a_recommendation(self):
        for state in ("disconnected", "not-installed", "unavailable"):
            with self.subTest(state=state):
                output, probe = self.run_list(network={"status": state, "ipv4": ""})
                probe.assert_called_once_with()
                self.assertNotIn("(recommended)", output)
                self.assertNotIn("Tailscale is connected", output)
                self.assertIn("Other / unverified:", output)

    def test_stopped_or_unknown_service_does_not_get_recommended_label(self):
        for state in ("stopped", "loaded", "unknown"):
            with self.subTest(state=state):
                output, _ = self.run_list(service=state)
                self.assertNotIn("(recommended)", output)
                self.assertIn(f"(server status: {state})", output)

    def test_loopback_bound_instance_shows_why_tailscale_is_unavailable(self):
        self.add_instance("work", 7851, "127.0.0.1")
        output, _ = self.run_list()
        self.assertIn("Tailscale / other networks: Unavailable: this server is bound to this machine only.", output)
        self.assertNotIn("http://100.100.10.20:7851 (recommended)", output)

    def test_no_argument_list_matches_explicit_list(self):
        explicit, _ = self.run_list()
        implicit, _ = self.run_list(args=[])
        self.assertEqual(explicit, implicit)

    def test_info_keeps_existing_json_shape_without_a_new_network_probe(self):
        import json
        output, probe = self.run_list(args=["info", "work"])
        probe.assert_not_called()
        self.assertEqual(json.loads(output)["addresses"], URLS)

    def test_empty_list_does_not_probe_network(self):
        empty = self.home / "empty"
        empty.mkdir(mode=0o700)
        self.registry = instances.Registry(empty)
        output, probe = self.run_list()
        probe.assert_not_called()
        self.assertIn("No installations found", output)

    def test_pending_instance_without_config_does_not_claim_a_connection(self):
        item = instances.Instance("pending", self.home)
        with self.registry.locked():
            self.registry.save(item, "pending", 7852)
        output = io.StringIO()
        with patch.object(instances, "service_status", return_value="stopped"), contextlib.redirect_stdout(output):
            instances.show(item, network=CONNECTED)
        self.assertIn("No saved network configuration", output.getvalue())
        self.assertNotIn("http://", output.getvalue())
