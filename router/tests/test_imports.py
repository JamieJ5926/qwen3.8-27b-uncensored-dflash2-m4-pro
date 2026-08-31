import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("dflash", "mlx_lm", "server")


class ImportGuardTests(unittest.TestCase):
    def test_router_modules_do_not_import_model_stack(self) -> None:
        files = list(ROOT.glob("*.py"))
        self.assertTrue(files)
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertNotIn(name, FORBIDDEN, f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
