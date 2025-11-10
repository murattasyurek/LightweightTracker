class LwBuffer:
    """    Multiprocessing single-producer / single-consumer ring buffer.    - Kapasite sabit; dolunca en eskiyi EZER (lossy).    - Frame: sabit boy (H,W,C), dtype=uint8 (BGR).    - Zaman damgası: sabit uzunluklu UTF-8 string (örn: 'YYYY-MM-DD--HH:MM:SS.mmm').    """    def __init__(self, capacity: int, shape=(1080, 1920, 3), ts_maxlen: int = 32):
        assert capacity > 0        self.N = int(capacity)
        self.shape = tuple(shape)
        self.H, self.W, self.C = self.shape
        self.frame_elems = self.H * self.W * self.C
        self.frame_bytes = self.frame_elems  # uint8        # Paylaşımlı ham bellek (frame pikselleri) ve timestamp alanı        self.buf    = RawArray('B', self.N * self.frame_bytes)
        self.ts_bytes = int(ts_maxlen)              # bir slot için ts byte kapasitesi        self.tsbuf  = RawArray('B', self.N * self.ts_bytes)

        # Paylaşımlı sayaç/işaretçiler        self.head   = Value('I', 0)   # bir sonraki yazma indeksi        self.count  = Value('I', 0)   # içerideki öğe sayısı        self.dropped = Value('Q', 0)  # ezilen (drop) toplam adet (64-bit)        # Senkronizasyon (aynı lock'u paylaşan Condition)        self.lock = Lock()
        self.cond = Condition(self.lock)

    # ---------- yardımcılar ----------    def _slot_view(self, idx):
        """Paylaşımlı frame buffer üzerinde slot 'idx' için numpy VIEW (kopyasız)."""        start = idx * self.frame_bytes
        mv = memoryview(self.buf)[start:start + self.frame_bytes]
        arr = np.frombuffer(mv, dtype=np.uint8, count=self.frame_elems)
        return arr.reshape(self.shape)

    def _write_ts(self, idx, s: str):
        """ts_str'i sabit uzunluklu byte alanına (null-terminated) yaz (loop ile)."""        b = s.encode('utf-8', 'ignore')
        if len(b) >= self.ts_bytes:
            b = b[:self.ts_bytes - 1]  # sona null için yer bırak        start = idx * self.ts_bytes
        # içerik        for i, val in enumerate(b):
            self.tsbuf[start + i] = val
        # pad (null)        for i in range(len(b), self.ts_bytes):
            self.tsbuf[start + i] = 0    def _read_ts(self, idx) -> str:
        """Sabit alanı null'a kadar okuyup UTF-8 string'e çevir."""        start = idx * self.ts_bytes
        out = bytearray()
        for i in range(self.ts_bytes):
            v = self.tsbuf[start + i]
            if v == 0:
                break            out.append(v if isinstance(v, int) else ord(v))
        return out.decode('utf-8', 'ignore')

    def _oldest_index(self):
        return (self.head.value - self.count.value) % self.N

    # ---------- durum ----------    def size(self) -> int:
        with self.lock:
            return int(self.count.value)

    def capacity(self) -> int:
        return self.N

    def dropped_total(self) -> int:
        with self.lock:
            return int(self.dropped.value)

    # ---------- ÜRETİCİ ----------    def offer(self, frame, ts_str: str):
        """        Yeni kare yazar. Kapasite doluysa EN ESKİSİNİ EZER (drop sayacı artar).        frame: HxWxC, uint8        ts_str: 'YYYY-MM-DD--HH:MM:SS.mmm' gibi kısa bir string        """        f = np.ascontiguousarray(frame, dtype=np.uint8)
        with self.cond:
            idx = self.head.value

            # doluyken yazmak en eskiyi ezecek → drop say            if self.count.value == self.N:
                self.dropped.value += 1            # frame kopyası + timestamp yaz            view = self._slot_view(idx)
            np.copyto(view, f, casting='no')
            self._write_ts(idx, ts_str)

            # head ileri            self.head.value = (idx + 1) % self.N
            # count arttır; doluysa N'de sabit kalır            if self.count.value < self.N:
                self.count.value += 1            self.cond.notify()

    # ---------- TÜKETİCİ ----------    def get(self, timeout_ms=0):
        """        Tek öğe çeker (FIFO). Yoksa Empty yükseltir.        Döndürdüğü frame KOPYA'dır, timestamp string'tir.        """        with self.cond:
            if self.count.value == 0 and timeout_ms > 0:
                self.cond.wait(timeout_ms / 1000.0)
            if self.count.value == 0:
                raise Empty()

            idx = (self.head.value - self.count.value) % self.N
            view = self._slot_view(idx)
            ts_str = self._read_ts(idx)

            # kopyayı kilit altındayken al → yarış yok            frame_copy = view.copy()
            # slotu tüket            self.count.value -= 1        return frame_copy, ts_str

    def drain(self, k: int, first_wait_ms=12):
        """        En fazla k öğeyi **tüketerek** alır (FIFO).        İlk öğe için kısa bekleme yapar, sonra non-blocking akar.        """        if k <= 0:
            return [], []

        frames, times = [], []
        with self.cond:
            if self.count.value == 0 and first_wait_ms > 0:
                self.cond.wait(first_wait_ms / 1000.0)

            take = min(k, self.count.value)
            idx = (self.head.value - self.count.value) % self.N

            for _ in range(take):
                view = self._slot_view(idx)
                t_str = self._read_ts(idx)

                # kopyayı kilit altındayken al                frames.append(view.copy())
                times.append(t_str)

                # slot tüketimi                self.count.value -= 1                idx = (idx + 1) % self.N

        return frames, times

    def snapshot_last(self, k: int):
        """        Son k öğeyi **TÜKETMEDEN** verir (geri bakış). KOPYA döndürür.        """        frames, times = [], []
        with self.lock:
            to_copy = min(k, self.count.value)
            start = (self.head.value - to_copy) % self.N
            idx = start
            for _ in range(to_copy):
                view = self._slot_view(idx)
                t_str = self._read_ts(idx)
                frames.append(view.copy())
                times.append(t_str)
                idx = (idx + 1) % self.N
        return frames, times

from typing import List, Dict, Tuple

class LwTracker:
    """    Basit ama sağlamlaştırılmış çoklu-nesne takipçisi + Doğrulama (min_hits).    - min_hits: Bir iz (track) aynı nesneyi en az bu kadar kez gördüğünde "confirmed" olur.    - suppress_unconfirmed=True ise confirmed olmayan izler çıktı listesine hiç eklenmez.    - include_provisional_id=True ile confirmed olmayana 'provisional_id' ekleyebilirsin.    """    def __init__(
        self,
        iou_thresh: float = 0.30,
        max_center_dist: float = 200.0,
        max_age: int = 30,
        min_iou_if_dist: float = 0.08,
        ema_momentum: float = 0.7,
        sort_key: str = "conf",
        gate_age_gain: float = 0.15,
        gate_age_cap: int = 4,
        small_area_px: int = 32 * 32,
        iou_small: float = 0.12,
        sticky_age: int = 3,
        sticky_iou_bonus: float = 0.18,
        min_box_area: int = 6 * 6,

        # ✅ YENİ        min_hits: int = 2,
        suppress_unconfirmed: bool = True,
        include_provisional_id: bool = False,
        provisional_key: str = "provisional_id",
    ):
        self.iou_thresh = float(iou_thresh)
        self.base_center_dist = float(max_center_dist)
        self.max_age = int(max_age)
        self.min_iou_if_dist = float(min_iou_if_dist)
        self.ema_momentum = float(ema_momentum)
        self.sort_key = sort_key

        self.gate_age_gain = float(gate_age_gain)
        self.gate_age_cap = int(gate_age_cap)

        self.small_area_px = int(small_area_px)
        self.iou_small = float(iou_small)

        self.sticky_age = int(sticky_age)
        self.sticky_iou_bonus = float(sticky_iou_bonus)

        self.min_box_area = int(min_box_area)

        # ✅ doğrulama/çıktı kontrolü        self.min_hits = int(min_hits)
        self.suppress_unconfirmed = bool(suppress_unconfirmed)
        self.include_provisional_id = bool(include_provisional_id)
        self.provisional_key = str(provisional_key)

        self.next_id = 1        # track sözlüğü:        # {'bbox':(x1,y1,x2,y2),'cls':int,'age':int,'vx':float,'vy':float,        #  'hits':int,'confirmed':bool}        self.tracks: Dict[int, Dict] = {}

    # ---- Yardımcılar ----    @staticmethod    def iou_xyxy(a: Tuple[float,float,float,float],
                 b: Tuple[float,float,float,float]) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1 = max(ax1, bx1);  iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2);  iy2 = min(ay2, by2)
        iw = max(0.0, ix2 - ix1); ih = max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0        aarea = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        barea = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = aarea + barea - inter + 1e-9        return inter / union

    @staticmethod    def center(b):
        x1, y1, x2, y2 = b
        return (0.5 * (x1 + x2), 0.5 * (y1 + y2))

    @staticmethod    def area(b):
        x1, y1, x2, y2 = b
        return max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))

    @staticmethod    def _shift_bbox(b, dx: float, dy: float):
        x1, y1, x2, y2 = b
        return (x1 + dx, y1 + dy, x2 + dx, y2 + dy)

    # ---- Eşleştirme (tek det için en iyi id) ----    def _assign_id(self, bbox, cls: int, used_tracks: set) -> int:
        cx, cy = self.center(bbox)
        best_id, best_raw_iou, best_eff_iou = None, 0.0, 0.0        best_dist, best_gate, best_size_ok = 1e9, 0.0, False        for tid, t in self.tracks.items():
            if t['cls'] != cls or tid in used_tracks:
                continue            vx = t.get('vx', 0.0); vy = t.get('vy', 0.0)
            pred_box = self._shift_bbox(t['bbox'], vx, vy)

            iou = self.iou_xyxy(bbox, pred_box)
            tcx, tcy = self.center(pred_box)
            dist = math.hypot(cx - tcx, cy - tcy)

            w = (t['bbox'][2] - t['bbox'][0]); h = (t['bbox'][3] - t['bbox'][1])
            diag = math.hypot(w, h)
            gate = max(self.base_center_dist, 0.35 * diag) * \
                   (1.0 + self.gate_age_gain * min(t['age'], self.gate_age_cap))

            dw = (bbox[2] - bbox[0]); dh = (bbox[3] - bbox[1])
            size_ratio = min(dw, w) / (max(dw, w) + 1e-6)
            ar_det = dw / (dh + 1e-6); ar_tr = w / (h + 1e-6)
            ar_diff = abs(ar_det - ar_tr) / (ar_tr + 1e-6)
            size_ok = (size_ratio >= 0.55) and (ar_diff <= 0.45)

            iou_eff = iou
            if t['age'] <= self.sticky_age and dist <= 0.5 * gate:
                iou_eff += self.sticky_iou_bonus

            better = (iou_eff > best_eff_iou) or (math.isclose(iou_eff, best_eff_iou) and dist < best_dist)
            if better:
                best_id = tid
                best_raw_iou = iou
                best_eff_iou = iou_eff
                best_dist = dist
                best_gate = gate
                best_size_ok = size_ok

        area_det = self.area(bbox)
        iou_th = self.iou_thresh if area_det >= self.small_area_px else min(self.iou_thresh, self.iou_small)

        if best_id is not None:
            ok_iou  = (best_eff_iou >= iou_th)
            ok_dist = (best_raw_iou >= max(self.min_iou_if_dist * 0.75, 0.05)) and (best_dist <= best_gate)
            ok_reacq = (best_dist <= 0.55 * best_gate) and best_size_ok and \
                       (self.tracks[best_id]['age'] <= self.sticky_age + 2)
            if ok_iou or ok_dist or ok_reacq:
                return best_id

        nid = self.next_id
        self.next_id += 1        return nid

    # ---- Ana güncelleme ----    def update(self, dets: List[Dict]) -> List[Dict]:
        """        dets: [{'bbox':(x1,y1,x2,y2), 'cls':int, 'conf':float}, ...]        return: confirmed politikasına göre:          - suppress_unconfirmed=True  -> sadece confirmed olanlar {'id':tid, **det}          - aksi halde:              include_provisional_id=True -> {'provisional_id':tid, **det} (confirmed değilse)              include_provisional_id=False -> {'id':None, **det} (confirmed değilse)        """        dets = [d for d in dets if self.area(d['bbox']) >= self.min_box_area]

        # 1) yaşlandır        for t in self.tracks.values():
            t['age'] += 1        used_tracks = set()
        outputs: List[Dict] = []

        dets_sorted = sorted(
            dets,
            key=(lambda d: self.area(d['bbox'])) if self.sort_key == "area"                 else (lambda d: float(d.get('conf', 0.0))),
            reverse=True        )

        for d in dets_sorted:
            tid = self._assign_id(d['bbox'], d['cls'], used_tracks)

            cx, cy = self.center(d['bbox'])
            existed_before = (tid in self.tracks)

            if existed_before:
                pcx, pcy = self.center(self.tracks[tid]['bbox'])
                vx_old = self.tracks[tid].get('vx', 0.0)
                vy_old = self.tracks[tid].get('vy', 0.0)
                vx = self.ema_momentum * (cx - pcx) + (1.0 - self.ema_momentum) * vx_old
                vy = self.ema_momentum * (cy - pcy) + (1.0 - self.ema_momentum) * vy_old
                hits = self.tracks[tid].get('hits', 0) + 1                confirmed = self.tracks[tid].get('confirmed', False)
            else:
                vx = vy = 0.0                hits = 1                confirmed = False            # confirmed’a geçiş            if not confirmed and hits >= self.min_hits:
                confirmed = True            # track’i güncelle            self.tracks[tid] = {
                'bbox': d['bbox'], 'cls': d['cls'], 'age': 0,
                'vx': vx, 'vy': vy,
                'hits': hits, 'confirmed': confirmed
            }
            used_tracks.add(tid)

            # Çıktıya ekleme politikası            if confirmed:
                outputs.append({'id': tid, **d})
            else:
                if not self.suppress_unconfirmed:
                    if self.include_provisional_id:
                        out = {self.provisional_key: tid, **d}
                    else:
                        out = {'id': None, **d}
                    outputs.append(out)

        # 5) yaşlıları temizle        stale = [tid for tid, t in self.tracks.items() if t['age'] > self.max_age]
        for tid in stale:
            self.tracks.pop(tid, None)

        return outputs

    # ---- Yardımcılar ----    def reset(self):
        self.tracks.clear()
        self.next_id = 1    def get_tracks(self) -> Dict[int, Dict]:
        return self.tracks
