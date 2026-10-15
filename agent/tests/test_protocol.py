import unittest

from octg_agent.engine import protocol as P
from octg_agent.engine.validate import validate


class ProtocolTests(unittest.TestCase):
    def test_final_response_always_has_all_keys(self):
        r = P.build_response("req_1", "click", True, result={"a": 1})
        self.assertEqual(set(r), {"request_id", "success", "action", "result", "error", "verification", "risk", "duration_ms"})
        self.assertTrue(r["success"])
        self.assertIsNone(r["error"])

    def test_command_shape_matches_spec(self):
        c = P.make_command("abc123", "click", {"element_id": "el_004"})
        self.assertEqual(c, {"request_id": "abc123", "action": "click", "parameters": {"element_id": "el_004"}})

    def test_error_shape(self):
        e = P.make_error("UNSUPPORTED_PAGE", "This Chrome page cannot be controlled by the extension.")
        self.assertEqual(e, {"code": "UNSUPPORTED_PAGE", "message": "This Chrome page cannot be controlled by the extension."})

    def test_request_ids_are_unique(self):
        self.assertEqual(len({P.new_request_id() for _ in range(500)}), 500)

    def test_extension_response_validation(self):
        self.assertEqual(P.validate_extension_response({"request_id": "x", "success": True}), [])
        self.assertTrue(P.validate_extension_response({"success": True}))
        self.assertTrue(P.validate_extension_response({"request_id": "x", "success": False}))  # failure without error
        self.assertTrue(P.validate_extension_response({"request_id": "x", "success": "yes"}))
        self.assertTrue(P.validate_extension_response([]))


class ValidatorTests(unittest.TestCase):
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}, "s": {"type": "string", "enum": ["a", "b"]},
                                               "l": {"type": "array", "items": {"type": "string"}}, "u": {"type": ["string", "integer"]}},
              "required": ["n"], "additionalProperties": False}

    def test_ok(self):
        self.assertEqual(validate(self.schema, {"n": 3, "s": "a", "l": ["x"], "u": 4}), [])

    def test_required_type_range_enum_unknown(self):
        self.assertTrue(validate(self.schema, {}))
        self.assertTrue(validate(self.schema, {"n": "3"}))
        self.assertTrue(validate(self.schema, {"n": True}))  # bool is not an integer
        self.assertTrue(validate(self.schema, {"n": 9}))
        self.assertTrue(validate(self.schema, {"n": 1, "s": "z"}))
        self.assertTrue(validate(self.schema, {"n": 1, "l": [1]}))
        self.assertTrue(validate(self.schema, {"n": 1, "extra": 1}))
        self.assertTrue(validate(self.schema, {"n": 1, "u": 1.5}))

    def test_null_type_and_unknown_type_do_not_crash(self):
        s = {"type": "object", "properties": {"task": {"type": ["string", "null"]}}, "required": ["task"]}
        self.assertEqual(validate(s, {"task": None}), [])
        self.assertEqual(validate(s, {"task": "x"}), [])
        self.assertTrue(validate(s, {"task": 3}))
        self.assertTrue(validate({"type": "wibble"}, 1))


if __name__ == "__main__":
    unittest.main()
