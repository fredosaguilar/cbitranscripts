"""The Spanish file note must never silently fall back to English only."""
import ast
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
module = ast.parse((root / "client_note_email.py").read_text())
compose = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "compose")
for arg in compose.args.args:
    arg.annotation = None
compose.returns = None
compose.args.defaults = [ast.Constant(None) for _ in compose.args.defaults]

environment = {
    "_message": lambda *_: "English file note",
    "resolve_language": lambda _, tag: tag or "en",
    "translate": lambda *_: "Nota de archivo en español",
    "signature": lambda: "Licensed in Washington State and Oregon.",
}
exec(compile(ast.fix_missing_locations(ast.Module(body=[compose], type_ignores=[])), str(root), "exec"), environment)
transcript = SimpleNamespace()
spanish = environment["compose"](transcript, tag_language_code="es")
assert spanish.index("Nota de archivo") < spanish.index("English file note")
assert "Licensed in Washington State and Oregon." in spanish
assert "Nota de archivo" not in environment["compose"](transcript, tag_language_code="en")
environment["translate"] = lambda *_: None
try:
    environment["compose"](transcript, tag_language_code="es")
except ValueError:
    pass
else:
    raise AssertionError("Missing Spanish translation sent an English-only note")
print("Client note delivery language checks passed")
