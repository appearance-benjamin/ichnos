"""The command line: no command may break another."""

import ast
import unittest
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "psyscientist" / "__main__.py"


class Commands(unittest.TestCase):
    def test_no_local_import_shadows_a_module_name(self):
        """An import inside main() of a name main() also uses from the module top makes that name local to the
        whole function, so every other command using it fails with UnboundLocalError."""
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        top = {a.asname or a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        local = {a.asname or a.name for n in ast.walk(main) if isinstance(n, (ast.Import, ast.ImportFrom))
                 for a in n.names}
        self.assertEqual(top & local, set())


if __name__ == "__main__":
    unittest.main()
