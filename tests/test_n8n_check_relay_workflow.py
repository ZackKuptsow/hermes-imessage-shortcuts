"""What n8n/check_relay.py imports: the relay's own shape, aimed away from everything real, and
credentials of its own."""

from __future__ import annotations

import copy
import json
import unittest

from n8n_check_support import (
    CREDENTIAL_TYPE,
    DATA_TABLE_TYPE,
    HTTP_REQUEST_TYPE,
    PLUGIN_PORT,
    RUN_RECORD_TABLE_ID,
    SET_TYPE,
    STAND_IN_URL,
    WEBHOOK_TYPE,
    check_relay,
    nodes_of_type,
    template,
)


class CheckWorkflowTest(unittest.TestCase):
    """What gets imported: the relay's own shape, aimed away from everything real."""

    def build(self, gate_broken: str | None = None, url: str = STAND_IN_URL) -> dict:
        return check_relay.build_check_workflow(template(), url, RUN_RECORD_TABLE_ID, gate_broken)

    def test_it_never_names_the_real_plugin(self):
        self.assertNotIn(PLUGIN_PORT, json.dumps(self.build()))

    def test_a_template_it_cannot_redirect_is_refused(self):
        changed = template()
        changed["nodes"].append(copy.deepcopy(nodes_of_type(changed, HTTP_REQUEST_TYPE)[0]))
        changed["nodes"][-1]["name"] = "A second forward"
        changed["nodes"][-1]["parameters"]["url"] = "http://127.0.0.1:8645/imessage"
        with self.assertRaises(check_relay.CheckError):
            check_relay.build_check_workflow(changed, STAND_IN_URL, RUN_RECORD_TABLE_ID, None)

    def test_a_second_forward_to_anywhere_at_all_is_refused(self):
        # Only one request can be aimed at the stand-in, so a second would go wherever it says.
        changed = template()
        changed["nodes"].append(copy.deepcopy(nodes_of_type(changed, HTTP_REQUEST_TYPE)[0]))
        changed["nodes"][-1]["name"] = "A second forward"
        changed["nodes"][-1]["parameters"]["url"] = "http://127.0.0.1:9/elsewhere"
        with self.assertRaises(check_relay.CheckError):
            check_relay.build_check_workflow(changed, STAND_IN_URL, RUN_RECORD_TABLE_ID, None)

    def test_a_stand_in_on_the_plugins_port_is_refused(self):
        with self.assertRaises(check_relay.CheckError):
            self.build(url=f"http://127.0.0.1:{PLUGIN_PORT}/imessage")

    def test_it_has_its_own_name_id_and_path(self):
        workflow = self.build()
        self.assertEqual(workflow["name"], check_relay.CHECK_WORKFLOW_NAME)
        self.assertEqual(workflow["id"], check_relay.CHECK_WORKFLOW_ID)
        self.assertEqual(nodes_of_type(workflow, WEBHOOK_TYPE)[0]["parameters"]["path"], check_relay.CHECK_WEBHOOK_PATH)

    def test_its_webhook_has_an_id_so_it_answers_on_its_path(self):
        self.assertTrue(nodes_of_type(self.build(), WEBHOOK_TYPE)[0].get("webhookId"))

    def test_it_posts_to_the_stand_in_and_writes_to_the_table_it_is_given(self):
        workflow = self.build()
        self.assertEqual(nodes_of_type(workflow, HTTP_REQUEST_TYPE)[0]["parameters"]["url"], STAND_IN_URL)
        self.assertEqual(nodes_of_type(workflow, DATA_TABLE_TYPE)[0]["parameters"]["dataTableId"]["value"], RUN_RECORD_TABLE_ID)

    def test_its_routing_table_holds_the_made_up_sender_alone(self):
        field_value = nodes_of_type(self.build(), SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]["value"]
        self.assertEqual(json.loads(field_value), [check_relay.KNOWN_SENDER])

    def test_it_uses_its_own_credentials_by_id_and_neither_real_one(self):
        workflow = self.build()
        for node_type, identifier, name in ((WEBHOOK_TYPE, check_relay.CHECK_RELAY_CREDENTIAL_ID, check_relay.CHECK_RELAY_CREDENTIAL),
                                            (HTTP_REQUEST_TYPE, check_relay.CHECK_PLUGIN_CREDENTIAL_ID, check_relay.CHECK_PLUGIN_CREDENTIAL)):
            with self.subTest(node_type):
                self.assertEqual(nodes_of_type(workflow, node_type)[0]["credentials"][CREDENTIAL_TYPE], {"id": identifier, "name": name})
        serialised = json.dumps(workflow)
        for real_name in ("Shortcuts relay secret", "Hermes iMessage plugin secret"):
            self.assertNotIn(real_name, serialised)

    def test_the_template_is_left_as_it_was(self):
        original = template()
        given = copy.deepcopy(original)
        check_relay.build_check_workflow(given, STAND_IN_URL, RUN_RECORD_TABLE_ID, "ip-allowlist")
        self.assertEqual(given, original)

    def test_the_ip_allowlist_break_names_an_address_that_is_not_this_mac(self):
        options = nodes_of_type(self.build("ip-allowlist"), WEBHOOK_TYPE)[0]["parameters"]["options"]
        self.assertEqual(options["ipWhitelist"], check_relay.NOT_THIS_MAC)

    def test_the_routing_table_break_takes_the_known_sender_out(self):
        field_value = nodes_of_type(self.build("routing-table"), SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]["value"]
        self.assertNotIn(check_relay.KNOWN_SENDER, field_value)

    def test_an_unknown_break_is_refused(self):
        with self.assertRaises(check_relay.CheckError):
            self.build("no-such-gate")


class CredentialsTest(unittest.TestCase):
    def test_the_header_names_are_the_real_ones_unless_that_gate_is_the_one_broken(self):
        for gate_broken, expected in ((None, check_relay.RELAY_SECRET_HEADER), ("wrong-header", check_relay.WRONG_HEADER)):
            with self.subTest(gate_broken):
                credentials = check_relay.check_credentials("relay-value", "plugin-value", gate_broken)
                relay = next(entry for entry in credentials if entry["name"] == check_relay.CHECK_RELAY_CREDENTIAL)
                self.assertEqual(relay["data"], {"name": expected, "value": "relay-value"})

    def test_each_has_its_own_id_and_the_header_auth_type(self):
        credentials = check_relay.check_credentials("relay-value", "plugin-value", None)
        self.assertEqual({entry["id"] for entry in credentials}, {check_relay.CHECK_RELAY_CREDENTIAL_ID, check_relay.CHECK_PLUGIN_CREDENTIAL_ID})
        self.assertEqual({entry["type"] for entry in credentials}, {CREDENTIAL_TYPE})


if __name__ == "__main__":
    unittest.main()
