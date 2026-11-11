"""Load the plugin directory as a package named `hermes_shaperotator` (the folder name has a hyphen in Hermes)."""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_plugin():
    if "hermes_shaperotator" in sys.modules:
        return sys.modules["hermes_shaperotator"]
    spec = importlib.util.spec_from_file_location("hermes_shaperotator", os.path.join(ROOT, "__init__.py"),
                                                  submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_shaperotator"] = mod
    spec.loader.exec_module(mod)
    return mod
