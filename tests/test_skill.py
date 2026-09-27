"""Static checks that the skill follows the Agent Skills spec and Anthropic authoring guidance."""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, "skills", "godmode")


def frontmatter(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    fields = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()
    return fields, m.group(2)


class TestSkill(unittest.TestCase):
    def setUp(self):
        self.fields, self.body = frontmatter(os.path.join(SKILL_DIR, "SKILL.md"))

    def test_name_matches_directory_and_rules(self):
        name = self.fields["name"]
        self.assertEqual(name, os.path.basename(SKILL_DIR))
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)
        self.assertNotIn("claude", name)
        self.assertNotIn("anthropic", name)

    def test_description(self):
        d = self.fields["description"]
        self.assertTrue(0 < len(d) <= 1024, len(d))
        self.assertNotRegex(d, r"<[a-zA-Z/]")
        self.assertFalse(re.match(r"^(I|You)\b", d), "description must be third person")
        self.assertIn("Use", d)  # says when to use it

    def test_frontmatter_fields(self):
        self.assertEqual(self.fields.get("disable-model-invocation"), "true")
        self.assertIn("argument-hint", self.fields)
        self.assertIn("godmode_engine.py", self.fields.get("allowed-tools", ""))

    def test_body_size_and_references(self):
        self.assertLess(len(self.body.splitlines()), 500)
        links = re.findall(r"\]\((references/[^)]+)\)", self.body)
        self.assertTrue(links)
        for link in links:
            self.assertTrue(os.path.exists(os.path.join(SKILL_DIR, link)), link)
        for name in os.listdir(os.path.join(SKILL_DIR, "references")):
            with open(os.path.join(SKILL_DIR, "references", name), encoding="utf-8") as f:
                text = f.read()
            # one level deep: references never send the reader to further reference files
            self.assertNotRegex(text, r"\]\((?!http)[^)]*\.md\)", name)
            if len(text.splitlines()) > 100:
                self.assertIn("## Contents", text, name)

    def test_no_windows_paths(self):
        self.assertNotRegex(self.body, r"scripts\\\\")

    def test_plugin_manifests(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as f:
            plugin = json.load(f)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as f:
            market = json.load(f)
        self.assertEqual(plugin["name"], "godmode")
        self.assertRegex(plugin["version"], r"^\d+\.\d+\.\d+$")
        entry = next(p for p in market["plugins"] if p["name"] == "godmode")
        self.assertEqual(entry["source"], "./")
        self.assertEqual(entry.get("version", plugin["version"]), plugin["version"])
        from importlib import util
        spec = util.spec_from_file_location("gm", os.path.join(SKILL_DIR, "scripts", "godmode", "__init__.py"))
        mod = util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.__version__, plugin["version"])

    def test_evals(self):
        with open(os.path.join(ROOT, "evals", "evals.json")) as f:
            evals = json.load(f)
        self.assertGreaterEqual(len(evals), 3)
        for e in evals:
            for k in ("skills", "query", "expected_behavior"):
                self.assertIn(k, e)


if __name__ == "__main__":
    unittest.main()
