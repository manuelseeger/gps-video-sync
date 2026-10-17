"""IGC B-fix reader with invalid-fix exclusion and midnight unwrapping."""
from datetime import datetime
from pathlib import Path
import re
import numpy as np
from .models import Track

class IGCReader:
    def read(self, path: str | Path, *, altitude: str = "auto") -> Track:
        if altitude not in {"auto", "gps", "pressure"}:
            raise ValueError("altitude must be auto, gps, or pressure")
        rows = []
        day = None
        last_clock = None
        rollover = 0
        for line in Path(path).read_text(errors="replace").splitlines():
            match = re.match(r"H[FO]DTE\s*(?:DATE:)?\s*(\d{6})", line)
            if match and day is None:
                try:
                    day = datetime.strptime(match[1], "%d%m%y").date()
                except ValueError:
                    pass
            if not line.startswith("B") or len(line) < 35:
                continue
            try:
                h, m, s = int(line[1:3]), int(line[3:5]), int(line[5:7])
                if h > 23 or m > 59 or s > 59:
                    continue
                clock = h * 3600 + m * 60 + s
                # Only a large reverse jump is a midnight, not out-of-order data.
                if last_clock is not None and clock < last_clock - 43200:
                    rollover += 86400
                last_clock = clock
                t = clock + rollover
                if line[24] != "A" or line[14] not in "NS" or line[23] not in "EW":
                    continue
                lat = (int(line[7:9]) + int(line[9:14]) / 60000) * (1 if line[14] == "N" else -1)
                lon = (int(line[15:18]) + int(line[18:23]) / 60000) * (1 if line[23] == "E" else -1)
                if abs(lat) > 90 or abs(lon) > 180:
                    continue
                if rows and t <= rows[-1][0]:
                    continue
                rows.append((t, lat, lon, int(line[25:30]), int(line[30:35])))
            except ValueError:
                continue
        if len(rows) < 2:
            raise ValueError("IGC needs at least two valid increasing fixes")
        a = np.asarray(rows, dtype=float)
        choice = altitude
        if choice == "auto":
            choice = "pressure" if np.ptp(a[:, 3]) > 0 and np.any(a[:, 3] != 0) else "gps"
        return Track(a[:, 0], a[:, 1], a[:, 2], a[:, 3 if choice == "pressure" else 4], day, choice)
