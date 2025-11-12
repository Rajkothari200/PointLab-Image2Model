import os
import time
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4
from flask import Flask, render_template, request, send_from_directory, Response, jsonify, abort
from werkzeug.utils import secure_filename

# Import your preprocessing functions
import preprocessing  

# -------- Config --------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
WORK_DIR = BASE_DIR / "runs"
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
MAX_CONTENT_LENGTH = 1 * 1024 * 1024 * 1024
COLMAP_EXEC = "colmap"  

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# Stage definitions (label, folder_key)
PREPROCESS_STAGES = [
    ("Original Image", "images"),
    ("Histogram Equalized", "histogram_equalized"),
    ("Gaussian Blur", "gaussian_blur"),
    ("Sharpened", "sharpened"),
    ("Edges (Canny)", "edges"),
    ("Median Filtered", "median_filtered"),
    ("Morphological Cleaned", "morphology"),
    ("Final Processed", "final_processed"),
]


COLMAP_STAGES = [
    "Feature Extraction",
    "Matching",
    "Sparse Mapping",
    "Model Conversion",
    "Undistortion",
    "Dense & Fusion"
]


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def json_sse(ev: dict) -> str:
    return f"data: {json.dumps(ev)}\n\n"


def stream_sse(events_generator):
    def generate():
        for ev in events_generator:
            yield json_sse(ev)
    return Response(generate(), mimetype="text/event-stream")


def make_zip_for_stage(run_dir: Path, stage_folder: Path) -> Path:
    zip_dir = run_dir / "out" / "zips"
    zip_dir.mkdir(parents=True, exist_ok=True)
    base_name = zip_dir / stage_folder.name
    archive_path = shutil.make_archive(str(base_name), 'zip', root_dir=str(stage_folder))
    return Path(archive_path)


# -------- Routes --------
@app.route("/")
def index():
    preprocess_labels = [s[0] for s in PREPROCESS_STAGES]
    preprocess_keys = [s[1] for s in PREPROCESS_STAGES]
    return render_template(
        "index.html",
        preprocess_labels=preprocess_labels,
        preprocess_keys=preprocess_keys,
        colmap_stages=COLMAP_STAGES
    )

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/preprocessing")
def preprocessing_info():
    return render_template("preprocessing_info.html")

@app.route("/reconstruction")
def reconstruction_info():
    return render_template("reconstruction_info.html")


@app.route("/upload", methods=["POST"])
def upload_files():
    if "files[]" not in request.files:
        return jsonify({"error": "No files part"}), 400

    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    run_id = str(uuid4())[:8]
    run_dir = WORK_DIR / run_id
    images_dir = run_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for f in files:
        if f and allowed_file(f.filename):
            fn = secure_filename(f.filename)
            dest = images_dir / fn
            f.save(str(dest))
            saved.append(fn)

    if not saved:
        return jsonify({"error": "No allowed files uploaded"}), 400

    return jsonify({"run_id": run_id, "files": saved}), 200


@app.route("/download_stage/<run_id>/<stage_key>")
def download_stage(run_id, stage_key):
    run_dir = WORK_DIR / run_id
    if stage_key == "images":
        stage_folder = run_dir / "images"
    else:
        stage_folder = run_dir / "out" / stage_key

    if not stage_folder.exists() or not stage_folder.is_dir():
        return jsonify({"error": "Stage folder not found"}), 404

    try:
        archive_path = make_zip_for_stage(run_dir, stage_folder)
        # send file using positional args: directory, path
        return send_from_directory(str(archive_path.parent), archive_path.name, as_attachment=True)
    except Exception as e:
        return jsonify({"error": f"Failed to create zip: {e}"}), 500


@app.route("/runs/<run_id>/out/<path:filename>")
def serve_run_out(run_id, filename):
    root = WORK_DIR / run_id / "out"
    full = root / filename
    if not full.exists():
        abort(404)
    return send_from_directory(str(root), filename, as_attachment=False)


@app.route("/runs/<run_id>/images/<path:filename>")
def serve_run_images(run_id, filename):
    root = WORK_DIR / run_id / "images"
    full = root / filename
    if not full.exists():
        abort(404)
    return send_from_directory(str(root), filename, as_attachment=False)


@app.route("/runs/<run_id>/out/download/<path:filename>")
def download_run_file(run_id, filename):
    root = WORK_DIR / run_id / "out"
    full = root / filename
    if not full.exists():
        abort(404)
    return send_from_directory(str(root), filename, as_attachment=True)


@app.route("/stream/<run_id>")
def stream_run(run_id):
    run_dir = WORK_DIR / run_id
    images_dir = run_dir / "images"
    out_root = run_dir / "out"
    out_root.mkdir(parents=True, exist_ok=True)

    if not images_dir.exists():
        return jsonify({"error": "run not found"}), 404

    def process_generator(): #Colmap
        # queued / start
        yield {"type": "status", "message": "Run queued", "progress": 0}
        yield {"type": "status", "message": "Starting preprocessing...", "progress": 2}

        # Collect images
        image_paths = sorted([p for p in images_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
        total_images = len(image_paths)
        if total_images == 0:
            yield {"type": "error", "message": "No images found", "progress": 0}
            return

        # Preprocess each image (this function should write all stage outputs under out_root)
        for i, img_path in enumerate(image_paths, start=1):
            try:
                preprocessing.preprocess_and_save_all_stages(img_path, out_root, max_size=2048)
                yield {
                    "type": "preprocess_image",
                    "message": f"Preprocessed {img_path.name} ({i}/{total_images})",
                    "progress": int(2 + 18 * (i / total_images)),
                    "image": f"/runs/{run_id}/out/final_processed/{img_path.name}"
                }
            except Exception as e:
                yield {"type": "error", "message": f"Preprocessing failed: {e}", "progress": 0}
                return

        # Preprocessing done, emit per-stage events with thumbnails
        yield {"type": "status", "message": "Preprocessing complete", "progress": 20}

        for label, folder in PREPROCESS_STAGES:
            thumbs = []
            if folder == "images":
                src_dir = run_dir / "images"
            else:
                src_dir = out_root / folder

            if src_dir.exists():
                for p in sorted(src_dir.iterdir())[:200]:
                    if folder == "images":
                        rel = f"/runs/{run_id}/images/{p.name}"
                    else:
                        rel = f"/runs/{run_id}/out/{folder}/{p.name}"
                    thumbs.append(rel)

            yield {
                "type": "stage_done",
                "group": "preprocessing",
                "stage_name": label,
                "stage_key": folder,
                "progress": 20,
                "thumbs": thumbs
            }

        # Prepare COLMAP workspace
        yield {"type": "status", "message": "Preparing COLMAP workspace...", "progress": 22}

        db_dir = out_root / "database"
        sparse_dir = out_root / "sparse"
        dense_dir = out_root / "dense"

        for p in (db_dir, sparse_dir, dense_dir):
            if p.exists():
                shutil.rmtree(p)
            p.mkdir(parents=True, exist_ok=True)

        image_path_for_colmap = out_root / "final_processed"
        if not image_path_for_colmap.exists():
            yield {"type": "error", "message": "final_processed folder missing", "progress": 0}
            return

        def run_cmd(cmd, progress_start, progress_end, colmap_stage_name=None):
            yield {"type": "status", "message": f"Running: {' '.join(cmd)}", "progress": progress_start}
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            while True:
                line = proc.stdout.readline()
                if line == "" and proc.poll() is not None:
                    break
                if line:
                    yield {"type": "log", "message": line.strip(), "progress": progress_start}
            rc = proc.wait()
            if rc != 0:
                yield {"type": "error", "message": f"Command {' '.join(cmd)} exited with code {rc}", "progress": progress_start}
                return False
            if colmap_stage_name:
                yield {"type": "stage_done", "group": "colmap", "stage_name": colmap_stage_name, "progress": progress_end}
            yield {"type": "status", "message": f"{colmap_stage_name or 'command'} finished", "progress": progress_end}
            return True

        # 1: features # COLMAP
        fe_cmd = [COLMAP_EXEC, "feature_extractor", "--database_path", str(db_dir / "database.db"), "--image_path", str(image_path_for_colmap)]
        for ev in run_cmd(fe_cmd, 22, 30, colmap_stage_name=COLMAP_STAGES[0]): 
            yield ev
            if ev.get("type") == "error": return

        # 2: matching
        em_cmd = [COLMAP_EXEC, "exhaustive_matcher", "--database_path", str(db_dir / "database.db")]
        for ev in run_cmd(em_cmd, 30, 40, colmap_stage_name=COLMAP_STAGES[1]):
            yield ev
            if ev.get("type") == "error": return

        # 3: mapper (sparse)
        map_cmd = [COLMAP_EXEC, "mapper", "--database_path", str(db_dir / "database.db"), "--image_path", str(image_path_for_colmap), "--output_path", str(sparse_dir)]
        for ev in run_cmd(map_cmd, 40, 55, colmap_stage_name=COLMAP_STAGES[2]):
            yield ev
            if ev.get("type") == "error": return

        # Find model index (0 or first directory)
        model_index = None
        top_models = [p for p in sparse_dir.iterdir() if p.is_dir()]
        if top_models:
            model_index = top_models[0]
        else:
            model_index = sparse_dir / "0"

        if not model_index.exists():
            yield {"type": "error", "message": "No sparse model found after mapper", "progress": 55}
            return

        # 4: model_converter (ensure output folder exists, explicit BIN input)
        top_models = list((sparse_dir).iterdir())
        model_index = None
        for sub in top_models:
            if sub.is_dir():
                # assume it's an index folder (0, 1, ...)
                model_index = sub
                break
        if model_index is None or not model_index.exists():
            # in some colmap versions output is ~/sparse/0
            model_index = sparse_dir / "0"
            if not model_index.exists():
                yield {"stage": "error", "message": "No sparse model found after mapper", "progress": 55}
                return

        model_converter_out = sparse_dir / "0_txt"
        model_converter_out.mkdir(parents=True, exist_ok=True)

        converter_cmd = [
            COLMAP_EXEC, "model_converter",
            "--input_path", str(model_index),
            "--output_path", str(model_converter_out),
            "--output_type", "TXT"
        ]
        for ev in run_cmd(converter_cmd, "model_converter", 55, 58):
            yield ev
            if ev.get("stage","").startswith("error"):
                return

        # 5: image_undistorter
        undist_cmd = [
            COLMAP_EXEC, "image_undistorter",
            "--image_path", str(image_path_for_colmap),
            "--input_path", str(model_index),
            "--output_path", str(dense_dir),
            "--output_type", "COLMAP"
        ]
        for ev in run_cmd(undist_cmd, 58, 65, colmap_stage_name=COLMAP_STAGES[4]):
            yield ev
            if ev.get("type") == "error": return

        # 6: patch_match_stereo
        pm_cmd = [
            COLMAP_EXEC, "patch_match_stereo",
            "--workspace_path", str(dense_dir),
            "--workspace_format", "COLMAP",
            "--PatchMatchStereo.max_image_size", "1600",
            "--PatchMatchStereo.num_iterations", "3",
            "--PatchMatchStereo.num_samples", "10",
            "--PatchMatchStereo.geom_consistency", "true"
        ]
        for ev in run_cmd(pm_cmd, 65, 85, colmap_stage_name=COLMAP_STAGES[5]):
            yield ev
            if ev.get("type") == "error": return

        # 7: stereo_fusion
        fuse_cmd = [
            COLMAP_EXEC, "stereo_fusion",
            "--workspace_path", str(dense_dir),
            "--workspace_format", "COLMAP",
            "--input_type", "geometric",
            "--output_path", str(dense_dir / "fused.ply")
        ]
        for ev in run_cmd(fuse_cmd, 85, 95, colmap_stage_name="Fusion (dense)"):
            yield ev
            if ev.get("type") == "error": return

        fused_ply = dense_dir / "fused.ply"
        if fused_ply.exists():
            yield {"type": "done", "message": "Reconstruction complete", "progress": 100, "fused_ply": f"/runs/{run_id}/out/dense/fused.ply"}
        else:
            yield {"type": "error", "message": "fused.ply not found after fusion", "progress": 95}
            return

    return stream_sse(process_generator())


# Run the app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
