"""Country display metadata, not an allow-list of supported legal systems."""

from collections.abc import Iterable

# Preserve the original pilot labels and ordering, including empty pilot libraries.
# Further countries are discovered from records/configuration, not added here.
COUNTRY_NAMES: dict[str, tuple[str, str]] = {
    "SG": ("新加坡", "Republic of Singapore"),
    "EG": ("埃及", "Arab Republic of Egypt"),
    "TN": ("突尼斯", "Republic of Tunisia"),
    "MA": ("摩洛哥", "Kingdom of Morocco"),
    "LB": ("黎巴嫩", "Republic of Lebanon"),
}


def country_catalog(codes: Iterable[str], site_metadata: Iterable[tuple[str, dict]]) -> list[dict]:
    """Unknown/conflicting display names fall back to the code, never inferred law facts."""
    discovered = {code.upper() for code in codes}
    metadata: dict[str, dict[str, set[str]]] = {}
    for code, config in site_metadata:
        if not isinstance(config, dict):
            continue
        names = metadata.setdefault(code.upper(), {})
        for key in ("country_name_zh", "country_name_orig"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                names.setdefault(key, set()).add(value.strip())

    result = []
    for code in [*COUNTRY_NAMES, *sorted(discovered - COUNTRY_NAMES.keys())]:
        if code in COUNTRY_NAMES:
            zh, original = COUNTRY_NAMES[code]
        else:
            names = metadata.get(code, {})
            zh_values = names.get("country_name_zh", set())
            orig_values = names.get("country_name_orig", set())
            zh = next(iter(zh_values)) if len(zh_values) == 1 else code
            original = next(iter(orig_values)) if len(orig_values) == 1 else code
        result.append({"country_code": code, "country_name_zh": zh, "country_name_orig": original})
    return result
