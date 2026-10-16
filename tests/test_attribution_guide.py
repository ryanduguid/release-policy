"""Check the maintained caller example against immutable-event ownership."""

import re
import unittest
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / "docs/attribution-consumers.md"


class AttributionGuideTests(unittest.TestCase):
    def test_the_example_separates_pr_coalescing_from_immutable_events(self) -> None:
        group = re.search(r"(?m)^  group: (.+)$", GUIDE.read_text()).group(1)
        self.assertEqual(
            group,
            "attribution-${{ github.event_name }}-${{ github.event_name == 'pull_request_target' "
            "&& github.event.pull_request.number || github.run_id }}",
        )
        # These are the documented expression's selected fields. GitHub must
        # still qualify actual queue delivery; this fixture tests key identity.
        pushes = [
            {"event_name": "push", "run_id": value, "sha": "a" * 40, "ref": ref}
            for value, ref in ((10, "main"), (11, "feature"), (12, "release"))
        ]
        keys = {f"attribution-{event['event_name']}-{event['run_id']}" for event in pushes}
        self.assertEqual(len(keys), len(pushes))
        self.assertIn("cancel-in-progress: false", GUIDE.read_text())


if __name__ == "__main__":
    unittest.main()
