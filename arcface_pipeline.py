import os
import sys
import cv2
import time
import uuid
import numpy as np
import torch
import onnxruntime as ort
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from scipy.spatial.distance import cosine as cosine_distance
from ultralytics import YOLO
from facenet_pytorch import MTCNN
 
 
# ══════════════════════════════════════════════════════════════════
# CẤU HÌNH  —  chỉnh theo môi trường thực tế
# ══════════════════════════════════════════════════════════════════
DB_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "attendance_db",   # ← tên database PostgreSQL của bạn
    "user":     "postgres",
    "password": "312005",     # ← đổi thành password thật
}
 
# Đường dẫn model ArcFace (đã tải thủ công vào ~/.insightface)
ARCFACE_MODEL_PATH = os.path.join(
    os.path.expanduser("~"),
    ".insightface", "models", "buffalo_l", "w600k_r50.onnx"
)
 
SIMILARITY_THRESHOLD = 0.5   # cosine similarity >= này → cùng người
PERSON_CONF_THRESH   = 0.5   # YOLOv8 confidence >= này → có người
SNAPSHOT_COOLDOWN    = 3     # giây chờ giữa 2 lần chụp snapshot
CAMERA_INDEX         = 0     # 0 = webcam mặc định
 
 
# ══════════════════════════════════════════════════════════════════
# THIẾT BỊ CHẠY
# ══════════════════════════════════════════════════════════════════
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[Khởi động] Thiết bị: {DEVICE}")
 
 
# ══════════════════════════════════════════════════════════════════
# LOAD MODEL (chạy 1 lần lúc khởi động)
# ══════════════════════════════════════════════════════════════════
print("[Khởi động] Load YOLOv8...")
_yolo = YOLO("yolov8n.pt")
 
print("[Khởi động] Load MTCNN...")
_mtcnn = MTCNN(
    image_size=160,
    margin=0,
    min_face_size=20,
    thresholds=[0.6, 0.7, 0.7],
    factor=0.709,
    post_process=True,      # chuẩn hoá output về [-1, 1]
    device=DEVICE,
    keep_all=False,         # chỉ lấy 1 mặt rõ nhất
)
 
print("[Khởi động] Load ArcFace...")
if not os.path.exists(ARCFACE_MODEL_PATH):
    raise FileNotFoundError(
        f"Không tìm thấy model ArcFace tại:\n  {ARCFACE_MODEL_PATH}\n"
        "Hãy giải nén buffalo_l.zip vào thư mục ~/.insightface/models/buffalo_l/"
    )
_ort_providers = (
    ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if DEVICE == "cuda" else ["CPUExecutionProvider"]
)
_arcface = ort.InferenceSession(ARCFACE_MODEL_PATH, providers=_ort_providers)
_arcface_input = _arcface.get_inputs()[0].name
print(f"[Khởi động] ArcFace provider: {_arcface.get_providers()}")
print("[Khởi động] Tất cả model đã sẵn sàng.\n")
 
 
# ══════════════════════════════════════════════════════════════════
# BƯỚC 2: YOLOv8 — Detect Person
# ══════════════════════════════════════════════════════════════════
def detect_persons(frame: np.ndarray) -> list:
    """
    Phát hiện người trong frame camera.
    Trả về: list [[x1,y1,x2,y2], ...] — toạ độ bounding box từng người.
    """
    results = _yolo(frame, verbose=False)[0]
    boxes = []
    for box in results.boxes:
        if (int(box.cls[0]) == 0                        # class 0 = person (COCO)
                and float(box.conf[0]) >= PERSON_CONF_THRESH):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
            boxes.append([x1, y1, x2, y2])
    return boxes
 
 
# ══════════════════════════════════════════════════════════════════
# BƯỚC 3: Crop ROI
# ══════════════════════════════════════════════════════════════════
def crop_roi(image: np.ndarray, box: list, margin: float = 0.10) -> np.ndarray:
    """
    Cắt vùng ROI chứa người, mở rộng thêm margin (10%) mỗi phía
    để tránh cắt mất phần rìa khuôn mặt.
    """
    h, w = image.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    x1 = max(0, int(x1 - bw * margin))
    y1 = max(0, int(y1 - bh * margin))
    x2 = min(w, int(x2 + bw * margin))
    y2 = min(h, int(y2 + bh * margin))
    return image[y1:y2, x1:x2]
 
 
# ══════════════════════════════════════════════════════════════════
# BƯỚC 4: MTCNN — Detect + Align Face
# ══════════════════════════════════════════════════════════════════
def align_face(roi_bgr: np.ndarray):
    """
    MTCNN phát hiện khuôn mặt trong ROI và căn chỉnh (align) theo
    5 landmark (2 mắt, mũi, 2 khoé miệng).
 
    Trả về: torch.Tensor (3, 160, 160) chuẩn hoá [-1,1], hoặc None.
    """
    rgb = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2RGB)
    return _mtcnn(rgb)
 
 
# ══════════════════════════════════════════════════════════════════
# BƯỚC 5+6: ArcFace — Trích Embedding
# ══════════════════════════════════════════════════════════════════
def compute_embedding(face_tensor: torch.Tensor) -> np.ndarray:
    """
    Chuyển face_tensor (MTCNN output) → đầu vào ArcFace → embedding 512-d.
 
    Quá trình chuyển đổi:
      Tensor (3,160,160) [-1,1]  →  đảo chuẩn hoá  →  [0,255] uint8
      →  resize 112×112  →  BGR  →  chuẩn hoá ArcFace  →  (1,3,112,112) float32
    """
    # Tensor → numpy HWC [0,255]
    img = face_tensor.permute(1, 2, 0).cpu().numpy()
    img = (img * 128.0 + 127.5).clip(0, 255).astype(np.uint8)
 
    # Resize về 112×112 (chuẩn ArcFace), đổi RGB→BGR
    img = cv2.resize(img, (112, 112))
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
 
    # Chuẩn hoá theo ArcFace: (x - 127.5) / 128.0
    img = (img.astype(np.float32) - 127.5) / 128.0
 
    # HWC → CHW, thêm batch dim → (1, 3, 112, 112)
    inp = np.expand_dims(img.transpose(2, 0, 1), axis=0)
 
    output = _arcface.run(None, {_arcface_input: inp})
    return output[0].flatten()          # shape (512,)
 
 
# ══════════════════════════════════════════════════════════════════
# PIPELINE XỬ LÝ SNAPSHOT (Bước 2→6, RAM only)
# ══════════════════════════════════════════════════════════════════
def process_snapshot(snapshot: np.ndarray):
    """
    Xử lý 1 ảnh SNAPSHOT chụp từ camera:
        snapshot  →  detect person  →  crop ROI  →  align face  →  embedding
 
    ⚠ Snapshot và mọi dữ liệu trung gian (roi, face_tensor) chỉ tồn
      tại trong RAM trong thời gian hàm này chạy.
      KHÔNG ghi bất kỳ file nào ra đĩa.
      Tất cả tự giải phóng khi hàm return.
 
    Trả về:
        (embedding np.ndarray 512-d, person_box)  nếu tìm thấy mặt
        (None, None)                              nếu không tìm thấy
    """
    person_boxes = detect_persons(snapshot)
    if not person_boxes:
        return None, None
 
    for box in person_boxes:
        # Crop ROI — chỉ tồn tại trong RAM
        roi = crop_roi(snapshot, box)
 
        # MTCNN align — chỉ tồn tại trong RAM
        face_tensor = align_face(roi)
        del roi                         # giải phóng ROI ngay
 
        if face_tensor is not None:
            embedding = compute_embedding(face_tensor)
            del face_tensor             # giải phóng face tensor ngay
            return embedding, box
 
    return None, None
 
 
# ══════════════════════════════════════════════════════════════════
# CHUYỂN ĐỔI EMBEDDING ↔ BYTES  (PostgreSQL bytea)
# ══════════════════════════════════════════════════════════════════
def bytes_to_embedding(raw: bytes) -> np.ndarray:
    """Đọc embedding từ cột bytea → numpy float32 (512,)."""
    return np.frombuffer(raw, dtype=np.float32).copy()
 
 
def embedding_to_bytes(emb: np.ndarray) -> bytes:
    """Chuyển numpy float32 → bytes để INSERT vào cột bytea."""
    return emb.astype(np.float32).tobytes()
 
 
# ══════════════════════════════════════════════════════════════════
# POSTGRESQL — Load embedding đã đăng ký
# ══════════════════════════════════════════════════════════════════
def load_registered_embeddings(conn) -> dict:
    """
    Đọc toàn bộ embedding hợp lệ từ bảng face_embeddings,
    JOIN students để lấy thông tin sinh viên.
 
    Điều kiện lọc (đúng schema):
        face_embeddings.model_name = 'arcface'
        face_embeddings.is_valid   = true
        students.is_active         = true
 
    Trả về: dict {
        student_id (str): {
            "full_name":    str,
            "student_code": str,
            "research_id":  str,
            "embedding":    np.ndarray (512,)
        }
    }
    """
    sql = """
        SELECT
            s.id            AS student_id,
            s.full_name,
            s.student_code,
            s.research_id,
            fe.embedding    AS emb_bytes,
            fe.embedding_dim
        FROM public.face_embeddings fe
        JOIN public.students s ON fe.student_id = s.id
        WHERE fe.model_name = 'arcface'
          AND fe.is_valid   = true
          AND s.is_active   = true
        ORDER BY s.full_name
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
 
    registered = {}
    for row in rows:
        sid = str(row["student_id"])
        emb = bytes_to_embedding(bytes(row["emb_bytes"]))
        registered[sid] = {
            "full_name":    row["full_name"]    or "Chưa có tên",
            "student_code": row["student_code"] or "",
            "research_id":  row["research_id"]  or "",
            "embedding":    emb,
        }
 
    print(f"[Database] Đã load {len(registered)} sinh viên "
          f"có embedding ArcFace hợp lệ.")
    return registered
 
 
def get_session_info(conn, session_id: str) -> dict:
    """
    Lấy thông tin phiên điểm danh từ class_sessions + classes.
    Báo lỗi nếu session không tồn tại hoặc đã đóng (ended_at IS NOT NULL).
    """
    sql = """
        SELECT
            cs.id           AS session_id,
            cs.started_at,
            c.class_code,
            c.subject_name,
            c.academic_year,
            c.term
        FROM public.class_sessions cs
        JOIN public.classes c ON cs.class_id = c.id
        WHERE cs.id = %s::uuid
          AND cs.ended_at IS NULL
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(sql, (session_id,))
    row = cur.fetchone()
    cur.close()
 
    if row is None:
        raise ValueError(
            f"\n[Lỗi] Không tìm thấy phiên điểm danh đang mở với ID:\n"
            f"  {session_id}\n"
            "Kiểm tra lại: class_sessions.ended_at IS NULL"
        )
    return dict(row)
 
 
def get_enrolled_students(conn, session_id: str) -> set:
    """
    Lấy danh sách student_id đã đăng ký vào lớp của session này.
    Dùng để chỉ điểm danh sinh viên đúng lớp (không nhận diện sinh viên lớp khác).
    """
    sql = """
        SELECT ce.student_id::text
        FROM public.class_enrollments ce
        JOIN public.class_sessions cs ON cs.class_id = ce.class_id
        WHERE cs.id = %s::uuid
    """
    cur = conn.cursor()
    cur.execute(sql, (session_id,))
    rows = cur.fetchall()
    cur.close()
    return {row[0] for row in rows}
 
 
# ══════════════════════════════════════════════════════════════════
# BƯỚC 7: Cosine Similarity + Threshold
# ══════════════════════════════════════════════════════════════════
def find_best_match(query_emb: np.ndarray, registered: dict,
                    enrolled_ids: set):
    """
    So sánh cosine similarity giữa query_emb với toàn bộ embedding đã đăng ký.
    Chỉ xét sinh viên thuộc lớp (enrolled_ids).
 
    Trả về:
        (student_id, info_dict, similarity)  nếu similarity >= threshold
        (None, None, best_sim)               nếu không khớp ai
    """
    best_id   = None
    best_info = None
    best_sim  = -1.0
 
    for sid, info in registered.items():
        # Bỏ qua sinh viên không thuộc lớp này
        if sid not in enrolled_ids:
            continue
 
        sim = 1.0 - cosine_distance(query_emb, info["embedding"])
        if sim > best_sim:
            best_sim  = sim
            best_id   = sid
            best_info = info
 
    if best_sim >= SIMILARITY_THRESHOLD:
        return best_id, best_info, best_sim
    return None, None, best_sim
 
 
# ══════════════════════════════════════════════════════════════════
# POSTGRESQL — Ghi điểm danh
# ══════════════════════════════════════════════════════════════════
def record_attendance(conn, session_id: str,
                      student_id: str, confidence: float):
    """
    INSERT điểm danh vào bảng attendance_records.
 
    Dùng đúng kiểu dữ liệu theo schema:
        status     → attendance_status ENUM = 'PRESENT'
        confidence → double precision  ∈ [0, 1]
        detected_at→ timestamptz
 
    ON CONFLICT (session_id, student_id) DO NOTHING:
        Bảo vệ tầng DB — tránh duplicate dù set RAM bị bypass.
    """
    sql = """
        INSERT INTO public.attendance_records
            (session_id, student_id, status, confidence, detected_at)
        VALUES
            (%s::uuid, %s::uuid,
             'PRESENT'::public.attendance_status,
             %s,
             %s)
        ON CONFLICT (session_id, student_id) DO NOTHING
    """
    cur = conn.cursor()
    cur.execute(sql, (
        session_id,
        student_id,
        round(float(confidence), 6),     # double precision, giữ 6 chữ số
        datetime.now(timezone.utc),
    ))
    conn.commit()
    cur.close()
 
 
# ══════════════════════════════════════════════════════════════════
# VÒNG LẶP CHÍNH — Camera + Điểm danh
# ══════════════════════════════════════════════════════════════════
def run(session_id: str):
 
    # ── Kết nối PostgreSQL ──────────────────────────────────────
    print("[DB] Đang kết nối PostgreSQL...")
    conn = psycopg2.connect(**DB_CONFIG)
    print("[DB] Kết nối thành công.")
 
    # ── Kiểm tra session hợp lệ ─────────────────────────────────
    session = get_session_info(conn, session_id)
    print(f"\n[Session] Lớp        : {session['class_code']} — "
          f"{session['subject_name']}")
    print(f"[Session] Năm học    : {session['academic_year']} "
          f"| Kỳ: {session['term']}")
    print(f"[Session] Bắt đầu   : {session['started_at']}")
 
    # ── Load danh sách sinh viên đã đăng ký lớp ─────────────────
    enrolled_ids = get_enrolled_students(conn, session_id)
    print(f"[Session] Sĩ số đăng ký: {len(enrolled_ids)} sinh viên")
 
    # ── Load embedding đã đăng ký từ face_embeddings ────────────
    registered = load_registered_embeddings(conn)
 
    # Lọc: chỉ giữ sinh viên thuộc lớp VÀ có embedding
    eligible = {sid: info for sid, info in registered.items()
                if sid in enrolled_ids}
    print(f"[Database] Sinh viên thuộc lớp + có embedding: {len(eligible)} người")
 
    if not eligible:
        print("[!] Không có sinh viên nào đủ điều kiện. Dừng lại.")
        conn.close()
        return
 
    # ── Tập điểm danh trong phiên — RAM only ────────────────────
    # Set này tự xoá khi chương trình kết thúc, không bao giờ ghi ra đĩa
    attended: set[str] = set()    # set of student_id string
 
    # ── Mở camera ───────────────────────────────────────────────
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"[!] Không mở được camera (index={CAMERA_INDEX}).")
        conn.close()
        return
 
    print("\n" + "═" * 60)
    print("  🎓  HỆ THỐNG ĐIỂM DANH ĐÃ SẴN SÀNG")
    print(f"  Session  : {session_id[:18]}...")
    print(f"  Lớp      : {session['class_code']} — {session['subject_name']}")
    print(f"  Threshold: {SIMILARITY_THRESHOLD}")
    print(f"  Tổng SV  : {len(eligible)} người")
    print("  Nhấn  Q  để thoát")
    print("═" * 60 + "\n")
 
    last_snapshot_time = 0.0
 
    while True:
        ret, frame = cap.read()
        if not ret:
            print("[!] Mất kết nối camera.")
            break
 
        now     = time.time()
        display = frame.copy()
 
        # Preview: vẽ box xanh realtime quanh người phát hiện được
        preview_boxes = detect_persons(frame)
        for box in preview_boxes:
            cv2.rectangle(display,
                          (box[0], box[1]), (box[2], box[3]),
                          (0, 255, 0), 2)
 
        # ── Khi có người VÀ đủ thời gian cooldown → chụp snapshot ──
        if preview_boxes and (now - last_snapshot_time >= SNAPSHOT_COOLDOWN):
            last_snapshot_time = now
            ts = datetime.now().strftime("%H:%M:%S")
 
            # ═══ CHỤP SNAPSHOT — copy frame vào RAM ════════════
            snapshot = frame.copy()
            print(f"\n[{ts}] Phát hiện người → chụp snapshot")
 
            # ═══ XỬ LÝ SNAPSHOT (RAM only, không ghi đĩa) ══════
            #   Bước 2→6: detect → crop → align → embedding
            query_emb, face_box = process_snapshot(snapshot)
 
            # ═══ XOÁ SNAPSHOT NGAY SAU KHI TRÍCH EMBEDDING ════
            del snapshot
 
            if query_emb is None:
                print(f"[{ts}] Không phát hiện được khuôn mặt rõ.")
                continue
 
            # ═══ BƯỚC 7: So sánh với database ══════════════════
            matched_id, matched_info, similarity = find_best_match(
                query_emb, eligible, enrolled_ids
            )
 
            # ═══ XOÁ EMBEDDING TẠM NGAY SAU KHI SO SÁNH XONG ═
            del query_emb
 
            # ── Xử lý kết quả ───────────────────────────────────
            if matched_id is None:
                # Không nhận ra ai trong lớp
                label = f"UNKNOWN  sim={similarity:.3f}"
                color = (0, 0, 255)     # đỏ
                print(f"[{ts}] UNKNOWN — sim tốt nhất = {similarity:.4f}")
 
            elif matched_id in attended:
                # Đã điểm danh trong phiên này → bỏ qua hoàn toàn
                name = matched_info["full_name"]
                code = matched_info["student_code"]
                label = f"{name}  DA DIEM DANH"
                color = (0, 165, 255)   # cam
                print(f"[{ts}] {name} ({code}) đã điểm danh → bỏ qua")
 
            else:
                # ✓ Điểm danh thành công lần đầu
                name = matched_info["full_name"]
                code = matched_info["student_code"]
                label = f"{name} ({code})  PRESENT  {similarity:.3f}"
                color = (0, 255, 0)     # xanh lá
 
                print(f"[{ts}] ✓ PRESENT: {name} | {code} | "
                      f"sim={similarity:.4f}")
 
                # Ghi vào PostgreSQL — attendance_records
                record_attendance(conn, session_id, matched_id, similarity)
                print(f"       → Đã INSERT attendance_records (PRESENT)")
 
                # Đánh dấu đã điểm danh trong RAM
                # (tránh điểm danh 2 lần — lớp bảo vệ thứ 1)
                attended.add(matched_id)
 
            # Hiển thị nhãn tại vị trí khuôn mặt trên màn hình
            if face_box is not None:
                fx1, fy1, fx2, fy2 = face_box
                cv2.rectangle(display,
                              (fx1, fy1), (fx2, fy2), color, 3)
                cv2.putText(display, label,
                            (fx1, max(fy1 - 10, 20)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
 
        # Hiển thị trạng thái góc trái màn hình
        cv2.putText(display,
                    f"Diem danh: {len(attended)}/{len(eligible)}",
                    (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (255, 255, 0), 2)
        cv2.putText(display,
                    f"{session['class_code']} | Q=Thoat",
                    (10, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (200, 200, 200), 1)
 
        cv2.imshow("ArcFace — Diem Danh Tu Dong", display)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            print("\n[Hệ thống] Người dùng thoát.")
            break
 
    # ── Dọn dẹp toàn bộ ─────────────────────────────────────────
    cap.release()
    cv2.destroyAllWindows()
    attended.clear()        # xoá set RAM
    registered.clear()      # xoá embedding database khỏi RAM
    eligible.clear()
    conn.close()
 
    print(f"\n[Hệ thống] Kết thúc phiên. "
          f"Kết quả đã lưu trong bảng attendance_records.\n")
 
 
# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Cách dùng: python arcface_pipeline.py <session_id>")
        print("Ví dụ    : python arcface_pipeline.py "
              "550e8400-e29b-41d4-a716-446655440000")
        sys.exit(1)
 
    _sid = sys.argv[1].strip()
 
    # Validate định dạng UUID trước khi kết nối DB
    try:
        uuid.UUID(_sid)
    except ValueError:
        print(f"[Lỗi] session_id không đúng định dạng UUID: '{_sid}'")
        sys.exit(1)
 
    run(_sid)
