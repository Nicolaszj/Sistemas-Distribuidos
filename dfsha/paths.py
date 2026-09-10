ROOT = "/"


def normalize(path, cwd=ROOT):
    if not path.startswith("/"):
        path = f"{cwd.rstrip('/')}/{path}"

    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)

    return "/" + "/".join(parts)


def split(path):
    normalized = normalize(path)
    if normalized == ROOT:
        return ROOT, ""

    parent, _, name = normalized.rpartition("/")
    return parent or ROOT, name


def is_valid_name(name):
    return bool(name) and name not in (".", "..") and "/" not in name
