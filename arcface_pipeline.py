import sys
import os
import cv2
import numpy as np
import torch
import onnxruntime as ort
from scipy.spatial.distance import cosine
from ultralytics import YOLO
from facenet_pytorch import MTCNN
 
 
# THIẾT BỊ CHẠY
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[ArcFace Pipeline] Đang chạy trên thiết bị: {DEVICE}")
 
 
#YOLOv8 - Detect Person
yolo_model = YOLO("yolov8n.pt")
PERSON_CLASS_ID = 0
 
 
def detect_person(image: np.ndarray, conf_threshold: float = 0.5):
    results = yolo_model(image, verbose=False)[0]
    person_boxes = []
    for box in results.boxes:
        cls_id = int(box.cls[0])
        conf = float(box.conf[0])
        if cls_id == PERSON_CLASS_ID and conf >= conf_threshold:
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
            person_boxes.append([x1, y1, x2, y2])
    return person_boxes
 
 
#Crop ROI
def crop_roi(image: np.ndarray, box: list, margin: float = 0.1):
    h, w = image.shape[:2]
    x1, y1, x2, y2 = box
    box_w = x2 - x1
    box_h = y2 - y1
    x1 = max(0, int(x1 - box_w * margin))
    y1 = max(0, int(y1 - box_h * margin))
    x2 = min(w, int(x2 + box_w * margin))
    y2 = min(h, int(y2 + box_h * margin))
    return image[y1:y2, x1:x2]
 
 
#MTCNN - Detect + Align Face
mtcnn = MTCNN(
    image_size=160,
    margin=0,
    min_face_size=20,
    thresholds=[0.6, 0.7, 0.7],
    factor=0.709,
    post_process=True,
    device=DEVICE,
    keep_all=False,
)
 
 
def detect_and_align_face(roi_image: np.ndarray):
    rgb_image = cv2.cvtColor(roi_image, cv2.COLOR_BGR2RGB)
    return mtcnn(rgb_image)
 
 
# ArcFace -> Embedding
# Load trực tiếp file .onnx, KHÔNG dùng FaceAnalysis để tránh lỗi tải
# Đường dẫn đến file recognition model đã tải thủ công
ARCFACE_MODEL_PATH = os.path.join(
    os.path.expanduser("~"),
    ".insightface", "models", "buffalo_l", "w600k_r50.onnx"
)
 
if not os.path.exists(ARCFACE_MODEL_PATH):
    raise FileNotFoundError(
        f"Không tìm thấy model ArcFace tại: {ARCFACE_MODEL_PATH}\n"
        "Hãy đảm bảo đã giải nén buffalo_l.zip vào đúng thư mục."
    )
 
# Chọn provider phù hợp: CUDA nếu có GPU, CPU nếu không
_providers = (
    ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if DEVICE == "cuda"
    else ["CPUExecutionProvider"]
)
_arcface_session = ort.InferenceSession(ARCFACE_MODEL_PATH, providers=_providers)
_arcface_input_name = _arcface_session.get_inputs()[0].name
 
print(f"[ArcFace] Đã load model từ: {ARCFACE_MODEL_PATH}")
print(f"[ArcFace] Provider đang dùng: {_arcface_session.get_providers()}")
 
 
def face_tensor_to_arcface_input(face_tensor: torch.Tensor) -> np.ndarray:
 
    # CHW -> HWC, đảo ngược chuẩn hoá [-1,1] về [0,255]
    img = face_tensor.permute(1, 2, 0).cpu().numpy()
    img = (img * 128.0 + 127.5).clip(0, 255).astype(np.uint8)
 
    # Resize 160x160 -> 112x112 (chuẩn ArcFace)
    img = cv2.resize(img, (112, 112))
 
    # RGB -> BGR (insightface/ArcFace dùng BGR)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
 
    # HWC -> CHW, normalize về [0,1], thêm batch dimension -> (1,3,112,112)
    img = img.astype(np.float32).transpose(2, 0, 1)
    img = (img - 127.5) / 128.0
    img = np.expand_dims(img, axis=0)
    return img
 
 
def get_arcface_embedding(face_tensor: torch.Tensor) -> np.ndarray:
  
    arcface_input = face_tensor_to_arcface_input(face_tensor)
    outputs = _arcface_session.run(None, {_arcface_input_name: arcface_input})
    embedding = outputs[0].flatten()
    return embedding
 
 
#Cosine Similarity + Threshold
def cosine_similarity(embedding1: np.ndarray, embedding2: np.ndarray) -> float:
    return 1 - cosine(embedding1, embedding2)
 
 
def is_same_person(embedding1: np.ndarray, embedding2: np.ndarray, threshold: float = 0.5):
    sim = cosine_similarity(embedding1, embedding2)
    return sim >= threshold, sim
 
 
def get_embedding_from_image(image_path: str, conf_threshold: float = 0.5, debug_dir: str = "debug"):
    image = cv2.imread(image_path)
    if image is None:
        raise FileNotFoundError(f"Không đọc được ảnh: {image_path}")

    h, w = image.shape[:2]
    print(f"[DEBUG] {image_path}: kích thước ảnh gốc = {w}x{h}")

    os.makedirs(debug_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(image_path))[0]

    person_boxes = detect_person(image, conf_threshold)
    print(f"[DEBUG] {image_path}: YOLO tìm thấy {len(person_boxes)} box người (boxes={person_boxes})")

    if len(person_boxes) == 0:
        print(f"[ArcFace] Không phát hiện được người nào trong: {image_path}")
        # Fallback: YOLO không thấy "person" (thường gặp với ảnh chân dung cắt sát mặt).
        # Thử coi toàn bộ ảnh là ROI để MTCNN tự tìm mặt, thay vì bỏ cuộc luôn.
        print(f"[DEBUG] {image_path}: thử fallback dùng toàn bộ ảnh làm ROI")
        person_boxes = [[0, 0, w, h]]

    for i, box in enumerate(person_boxes):
        roi = crop_roi(image, box)
        roi_path = os.path.join(debug_dir, f"{base_name}_roi_{i}.jpg")
        cv2.imwrite(roi_path, roi)
        print(f"[DEBUG] {image_path}: đã lưu ROI #{i} -> {roi_path} (size={roi.shape[1]}x{roi.shape[0]})")

        face_tensor = detect_and_align_face(roi)
        if face_tensor is not None:
            print(f"[DEBUG] {image_path}: MTCNN tìm thấy mặt hợp lệ trong ROI #{i}")
            # Lưu lại ảnh mặt sau align để xem MTCNN thực sự "thấy" gì
            face_img = face_tensor.permute(1, 2, 0).cpu().numpy()
            face_img = (face_img * 128.0 + 127.5).clip(0, 255).astype(np.uint8)
            face_img = cv2.cvtColor(face_img, cv2.COLOR_RGB2BGR)
            face_path = os.path.join(debug_dir, f"{base_name}_face_{i}.jpg")
            cv2.imwrite(face_path, face_img)
            print(f"[DEBUG] {image_path}: đã lưu ảnh mặt sau align -> {face_path}")
            return get_arcface_embedding(face_tensor)
        else:
            print(f"[DEBUG] {image_path}: MTCNN KHÔNG tìm thấy mặt trong ROI #{i}")

    print(f"[ArcFace] Phát hiện người nhưng không tìm thấy mặt hợp lệ trong: {image_path}")
    return None
 
 

# DEMO
if __name__ == "__main__":
    if len(sys.argv) >= 3:
        img_path_1, img_path_2 = sys.argv[1], sys.argv[2]
    else:
        img_path_1 = "images/picture_1.jpg"
        img_path_2 = "images/picture_2.jpg"
        print(f"(Không truyền tham số, dùng ảnh mặc định: {img_path_1}, {img_path_2})")
 
    print("\n=== PIPELINE ARCFACE ===")
    emb1 = get_embedding_from_image(img_path_1)
    emb2 = get_embedding_from_image(img_path_2)
 
    if emb1 is not None and emb2 is not None:
        same, sim = is_same_person(emb1, emb2)
        print(f"Chiều embedding   : {emb1.shape}")
        print(f"Cosine similarity : {sim:.4f}")
        print(f"Kết luận          : {'CÙNG 1 NGƯỜI' if same else 'KHÁC NGƯỜI'}")
    else:
        print("Không trích xuất được embedding từ một trong hai ảnh.")