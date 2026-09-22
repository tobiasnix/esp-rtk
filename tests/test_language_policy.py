# SPDX-License-Identifier: AGPL-3.0-only
"""V11 language and translation contract gates."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import ast


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_EXTENSIONS = (".py", ".js", ".css", ".html", ".sh")


# i18n.js and this file itself must contain the words this gate looks for,
# since recognizing German text is their entire purpose. The other two
# candidates are files that exist only in the upstream repository (release
# tooling that must contain German samples); filtering by os.path.exists
# keeps the exclusion working there while leaving the public copy, which
# never contains those two files, unconfusing about paths it does not have.
CANDIDATE_EXCLUDED = ("i18n.js", "tests/test_language_policy.py",
                      "tools/export_public.py", "tests/test_export_public.py")


def _excluded_names(repo=REPO):
    return {name for name in CANDIDATE_EXCLUDED if os.path.exists(os.path.join(repo, name))}


def _is_git_checkout(repo=REPO):
    """True if `repo` has a .git directory, i.e. `git ls-files` will work.

    A release tarball has no such directory, so a test that shells out to
    git must not crash there.
    """
    return os.path.isdir(os.path.join(repo, ".git"))


def repository_sources():
    """Yield versioned source files covered by the English-code policy."""
    result = subprocess.run(
        ["git", "ls-files", "--", "*.py", "*.js", "*.css", "*.html", "*.sh"],
        cwd=REPO, text=True, capture_output=True, check=True,
    )
    excluded = _excluded_names()
    for relative in result.stdout.splitlines():
        if relative not in excluded and relative.endswith(SOURCE_EXTENSIONS):
            yield relative, os.path.join(REPO, relative)


class TestExcludedNames(unittest.TestCase):
    """(F6) the exclusion set is built from candidates filtered by existence,
    so the public copy (which never has the two upstream-only files) is not
    left referencing files it does not contain."""

    def repo_with(self, names):
        repo = tempfile.mkdtemp(prefix="esp-rtk-lang-repo-")
        self.addCleanup(shutil.rmtree, repo, True)
        for name in names:
            path = os.path.join(repo, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w", encoding="utf-8").close()
        return repo

    def test_only_existing_candidates_are_excluded(self):
        import test_language_policy as module
        repo = self.repo_with(["tests/test_language_policy.py"])
        self.assertEqual(module._excluded_names(repo), {"tests/test_language_policy.py"})

    def test_all_candidates_excluded_when_all_are_present(self):
        import test_language_policy as module
        repo = self.repo_with(module.CANDIDATE_EXCLUDED)
        self.assertEqual(module._excluded_names(repo), set(module.CANDIDATE_EXCLUDED))


class TestGitCheckoutDetection(unittest.TestCase):
    def test_a_plain_directory_is_not_a_git_checkout(self):
        repo = tempfile.mkdtemp(prefix="esp-rtk-nogit-")
        self.addCleanup(shutil.rmtree, repo, True)
        self.assertFalse(_is_git_checkout(repo))

    def test_a_directory_with_dot_git_is_a_git_checkout(self):
        repo = tempfile.mkdtemp(prefix="esp-rtk-git-")
        self.addCleanup(shutil.rmtree, repo, True)
        os.makedirs(os.path.join(repo, ".git"))
        self.assertTrue(_is_git_checkout(repo))


class TestCanonicalNames(unittest.TestCase):
    def test_removed_german_identifiers_do_not_return(self):
        forbidden = ("flug" + "schreiber", "werks" + "einstellung", "netzwerk" + "liste",
                     "fehler" + "log", "zugangstaste_pruefen", "zaehle_fixqualitaet",
                     "board_metriken", "netze_ordnen", "verbindung_uebernehmen")
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [item for item in dirs if item not in (".git", "backup", "__pycache__")]
            for name in files:
                if (not name.endswith((".py", ".js")) or
                        name in (os.path.basename(__file__), "i18n.js")):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().lower()
                for word in forbidden:
                    self.assertNotIn(word, text, "%s contains %s" % (path, word))

    def test_production_runtime_strings_are_not_german(self):
        german = re.compile(
            r"(?:[äöüß]|\b(?:der|die|das|den|dem|des|eine?|einer|einen|einem|"
            r"und|oder|aber|wenn|dann|sonst|ohne|fuer|ueber|zur|zum|wird|"
            r"werden|bleibt|kein|keine|nicht|hier|beim|damit|geraet|"
            r"konfiguration|antwort|pruefsumme|ungueltig|fehler)\b)", re.I)
        for name in os.listdir(REPO):
            if not name.endswith(".py"):
                continue
            path = os.path.join(REPO, name)
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), filename=path)
            docstrings = set()
            for owner in ast.walk(tree):
                if not isinstance(owner, (ast.Module, ast.ClassDef,
                                          ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if owner.body and isinstance(owner.body[0], ast.Expr):
                    value = owner.body[0].value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        docstrings.add(id(value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings):
                    self.assertIsNone(
                        german.search(node.value),
                        "%s:%s contains German runtime text" % (path, node.lineno),
                    )

    @unittest.skipUnless(_is_git_checkout(), "needs a git checkout")
    def test_all_source_text_is_english(self):
        """Reject German prose and identifiers anywhere in versioned source."""
        words = (
            "der die das den dem des und oder aber wenn dann sonst ohne fuer ueber "
            "zur zum wird werden bleibt kein keine nicht hier beim damit geraet "
            "konfiguration antwort pruefsumme ungueltig fehler ist sind passwort "
            "schluessel satz saetze zeichen zaehlt laeuft haengt quelle zeile zeilen "
            "meldung neustart vorher nachher eingabe ausgabe speichern laden verbindung "
            "netzwerk werte noetig geschuetzt erlaubt abgelehnt erwartet vorhanden "
            "befehl sitzung seite zugang daten speicher datei feld liste eintrag erfolg "
            "anfrage schreiben lesen startet beendet loescht behaelt erkennt enthaelt "
            "verwendet standardmaessig zufaellig gespeichert geheimnis gehalten abschalten "
            "kanalwechsel messwert absturz zaehler mehrere gleiche falsch richtig erste "
            "letzte waehrend zwischen innerhalb sofort erneut niemals bereits"
        ).split()
        pattern = re.compile(
            r"(?:[äöüß]|\b(?:%s)\b)" % "|".join(map(re.escape, words)), re.I
        )
        violations = []
        for relative, path in repository_sources():
            with open(path, encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    match = pattern.search(line)
                    if match:
                        violations.append(
                            "%s:%d: %s" % (relative, line_number, match.group(0))
                        )
        self.assertEqual([], violations, "German source text found:\n" + "\n".join(violations))


class TestTranslations(unittest.TestCase):
    @unittest.skipUnless(subprocess.run(["sh", "-c", "command -v node"],
                                        capture_output=True).returncode == 0,
                         "Node is required")
    def test_dictionaries_match_and_interpolation_is_escaped(self):
        script = r"""
const i=require('./i18n.js');
const en=Object.keys(i.dictionaries.en).sort(), de=Object.keys(i.dictionaries.de).sort();
if(JSON.stringify(en)!==JSON.stringify(de)) throw Error('dictionary mismatch');
i.setLanguage('en');
const escaped=i.t('sample',{value:'<script>'});
console.log(JSON.stringify({en,de,escaped}));
"""
        # Add the sample key equally without making the production dictionary
        # carry a test-only string: unknown keys still exercise interpolation.
        script = script.replace("const escaped=", "i.dictionaries.en.sample='{value}'; i.dictionaries.de.sample='{value}'; const escaped=")
        result = subprocess.run(["node", "-e", script], cwd=REPO,
                                text=True, capture_output=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["escaped"], "&lt;script&gt;")
        self.assertTrue(payload["en"])

    def test_no_unresolved_template_placeholders_in_dictionary_values(self):
        with open(os.path.join(REPO, "i18n.js"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("TODO", source)
        self.assertNotIn("TRANSLATE_ME", source)


if __name__ == "__main__":
    unittest.main()
