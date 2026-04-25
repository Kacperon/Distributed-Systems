import os
import uuid


class Session:
    def __init__(self, name):
        self.name = name
        self.path = f".session_{name}"
        self.id = self._load_or_create()

    def _load_or_create(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                sid = f.read().strip()
                if sid:
                    return sid
        sid = str(uuid.uuid4())
        with open(self.path, "w") as f:
            f.write(sid)
        return sid

    def clear(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
