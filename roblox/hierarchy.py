import os
from pathlib import Path
import xml.etree.ElementTree as ET

RBXMK_PATH = os.getenv("RBXMK_PATH", "rbxmk")
MAX_RBX_NODES = 800
MAX_RBX_DEPTH = 40

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"

RBXMK_PATH = TOOLS_DIR / "rbxmk"
RBX_HIERARCHY_LUA = TOOLS_DIR / "rbx_hierarchy.lua"

CLASS_ICONS = {
    "Model": "📦",
    "Folder": "📁",

    "Part": "🧱",
    "MeshPart": "🔷",
    "UnionOperation": "🔶",

    "Attachment": "📍",
    "WeldConstraint": "🔗",
    "Motor6D": "🦴",

    "Script": "📜",
    "LocalScript": "📜",
    "ModuleScript": "📘",

    "Humanoid": "🧍",
    "Animator": "🎞️",
}
DEFAULT_ICON = "▫️"

def _get_name_from_item(item: ET.Element):
    props = item.find("Properties")
    if props is None:
        return None

    for s in props.findall("string"):
        if s.attrib.get("name") == "Name":
            return s.text or ""

    for s in props.findall("ProtectedString"):
        if s.attrib.get("name") == "Name":
            return s.text or ""

    return None

def build_rbxmx_hierarchy_text(rbxmx_path: Path):
    tree = ET.parse(rbxmx_path)
    root = tree.getroot()

    lines = []
    node_count = 0
    truncated = False

    def walk(item: ET.Element, depth: int):
        nonlocal node_count, truncated

        if truncated:
            return

        if node_count >= MAX_RBX_NODES or depth >= MAX_RBX_DEPTH:
            truncated = True
            return

        node_count += 1

        class_name = item.attrib.get("class", "Instance")
        name = _get_name_from_item(item) or class_name

        indent = "  " * depth
        icon = CLASS_ICONS.get(class_name, DEFAULT_ICON)
        label = f"{name} ({class_name})" if name != class_name else class_name
        lines.append(f"{indent}{icon} {label}")

        for child in item.findall("Item"):
            walk(child, depth + 1)

    top_items = root.findall("Item")
    if not top_items:
        raise RuntimeError("RBXMX has no <Item> entries")

    for it in top_items:
        walk(it, 0)

    if truncated:
        lines.append("")
        lines.append(f"[truncated: limit reached (nodes={MAX_RBX_NODES}, depth={MAX_RBX_DEPTH})]")

    return "\n".join(lines), node_count, truncated