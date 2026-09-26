"""手写 JSON Schema 子集的参数校验测试。"""

from __future__ import annotations

import unittest

from app.tools.registry import InvalidArguments, validate_arguments

SCHEMA = {
    "type": "object",
    "properties": {
        "order_no": {
            "type": "string",
            "pattern": r"^SH\d{8}$",
            "maxLength": 12,
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        "status": {"type": "string", "enum": ["pending", "confirmed"]},
        "remark": {"type": "string", "maxLength": 5},
        "flags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    },
    "required": ["order_no"],
    "additionalProperties": False,
}


class ValidateArgumentsTests(unittest.TestCase):
    def test_valid_payload_passes(self) -> None:
        validate_arguments(
            SCHEMA,
            {
                "order_no": "SH20250001",
                "limit": 5,
                "status": "pending",
                "flags": ["a", "b"],
            },
        )

    def test_missing_required(self) -> None:
        with self.assertRaises(InvalidArguments) as ctx:
            validate_arguments(SCHEMA, {})
        self.assertEqual(ctx.exception.code, "invalid_arguments")
        self.assertIn("order_no", ctx.exception.message)

    def test_wrong_type(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(SCHEMA, {"order_no": "SH20250001", "limit": "5"})

    def test_bool_is_not_integer(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(SCHEMA, {"order_no": "SH20250001", "limit": True})

    def test_pattern(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(SCHEMA, {"order_no": "SH2025"})

    def test_enum(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(
                SCHEMA, {"order_no": "SH20250001", "status": "shipped"}
            )

    def test_max_length(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(
                SCHEMA, {"order_no": "SH20250001", "remark": "123456"}
            )

    def test_minimum_and_maximum(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(SCHEMA, {"order_no": "SH20250001", "limit": 0})
        with self.assertRaises(InvalidArguments):
            validate_arguments(SCHEMA, {"order_no": "SH20250001", "limit": 21})

    def test_unknown_properties_rejected(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(
                SCHEMA, {"order_no": "SH20250001", "unexpected": 1}
            )

    def test_array_items(self) -> None:
        with self.assertRaises(InvalidArguments):
            validate_arguments(
                SCHEMA, {"order_no": "SH20250001", "flags": ["a", 2]}
            )
        with self.assertRaises(InvalidArguments):
            validate_arguments(
                SCHEMA, {"order_no": "SH20250001", "flags": ["a", "b", "c"]}
            )

    def test_nullable_union(self) -> None:
        schema = {
            "type": "object",
            "properties": {"x": {"type": ["string", "null"]}},
        }
        validate_arguments(schema, {"x": None})
        validate_arguments(schema, {"x": "ok"})
        with self.assertRaises(InvalidArguments):
            validate_arguments(schema, {"x": 3})

    def test_non_object_schema_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_arguments({"type": "array"}, [])


if __name__ == "__main__":
    unittest.main()
