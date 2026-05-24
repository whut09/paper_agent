from flask import Flask, request, send_file
from celery import Celery, Task
from celery.result import AsyncResult
from paper_agent import translate_stream
import tqdm
import json
import io
import tempfile
from pathlib import Path
from string import Template
from paper_agent.doclayout import ModelInstance
from paper_agent.config import ConfigManager
from paper_agent.paper_summary import DEFAULT_MAX_ASSETS, summarize_paper

flask_app = Flask("paper_agent")
flask_app.config.from_mapping(
    CELERY=dict(
        broker_url=ConfigManager.get("CELERY_BROKER", "redis://127.0.0.1:6379/0"),
        result_backend=ConfigManager.get("CELERY_RESULT", "redis://127.0.0.1:6379/0"),
    )
)


def celery_init_app(app: Flask) -> Celery:
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(app.name)
    celery_app.config_from_object(app.config["CELERY"])
    celery_app.Task = FlaskTask
    celery_app.set_default()
    celery_app.autodiscover_tasks()
    app.extensions["celery"] = celery_app
    return celery_app


celery_app = celery_init_app(flask_app)


@celery_app.task(bind=True)
def translate_task(
    self: Task,
    stream: bytes,
    args: dict,
):
    def progress_bar(t: tqdm.tqdm):
        self.update_state(state="PROGRESS", meta={"n": t.n, "total": t.total})  # noqa
        print(f"Translating {t.n} / {t.total} pages")

    if "prompt" in args:
        args["prompt"] = Template(args["prompt"])

    doc_mono, doc_dual = translate_stream(
        stream,
        callback=progress_bar,
        model=ModelInstance.value,
        **args,
    )
    return doc_mono, doc_dual


@celery_app.task(bind=True)
def summarize_task(
    self: Task,
    stream: bytes,
    filename: str,
    args: dict,
):
    def progress_bar(value: float, desc: str):
        self.update_state(state="PROGRESS", meta={"n": value, "total": 1, "desc": desc})
        print(f"Summarizing {filename}: {desc}")

    with tempfile.TemporaryDirectory(prefix="paper_summary_") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / filename
        input_path.write_bytes(stream)
        output_dir = tmp_path / "output"
        docx_path = summarize_paper(
            str(input_path),
            output_dir,
            pages=args.get("pages"),
            summary_language=args.get("summary_language", "中文"),
            codex_envs=args.get("codex_envs", {}),
            max_assets=int(args.get("max_assets", DEFAULT_MAX_ASSETS)),
            progress=progress_bar,
        )
        return Path(docx_path).read_bytes()


@flask_app.route("/v1/translate", methods=["POST"])
def create_translate_tasks():
    file = request.files["file"]
    stream = file.stream.read()
    print(request.form.get("data"))
    args = json.loads(request.form.get("data"))
    task = translate_task.delay(stream, args)
    return {"id": task.id}


@flask_app.route("/v1/summarize", methods=["POST"])
def create_summarize_tasks():
    file = request.files["file"]
    stream = file.stream.read()
    args = json.loads(request.form.get("data") or "{}")
    filename = Path(file.filename or "paper.pdf").name
    task = summarize_task.delay(stream, filename, args)
    return {"id": task.id}


@flask_app.route("/v1/translate/<id>", methods=["GET"])
def get_translate_task(id: str):
    result: AsyncResult = celery_app.AsyncResult(id)
    if str(result.state) == "PROGRESS":
        return {"state": str(result.state), "info": result.info}
    else:
        return {"state": str(result.state)}


@flask_app.route("/v1/summarize/<id>", methods=["GET"])
def get_summarize_task(id: str):
    result: AsyncResult = celery_app.AsyncResult(id)
    if str(result.state) == "PROGRESS":
        return {"state": str(result.state), "info": result.info}
    return {"state": str(result.state)}


@flask_app.route("/v1/translate/<id>", methods=["DELETE"])
def delete_translate_task(id: str):
    result: AsyncResult = celery_app.AsyncResult(id)
    result.revoke(terminate=True)
    return {"state": str(result.state)}


@flask_app.route("/v1/summarize/<id>", methods=["DELETE"])
def delete_summarize_task(id: str):
    result: AsyncResult = celery_app.AsyncResult(id)
    result.revoke(terminate=True)
    return {"state": str(result.state)}


@flask_app.route("/v1/translate/<id>/<format>")
def get_translate_result(id: str, format: str):
    result = celery_app.AsyncResult(id)
    if not result.ready():
        return {"error": "task not finished"}, 400
    if not result.successful():
        return {"error": "task failed"}, 400
    doc_mono, doc_dual = result.get()
    to_send = doc_mono if format == "mono" else doc_dual
    return send_file(io.BytesIO(to_send), "application/pdf")


@flask_app.route("/v1/summarize/<id>/docx")
def get_summarize_result(id: str):
    result = celery_app.AsyncResult(id)
    if not result.ready():
        return {"error": "task not finished"}, 400
    if not result.successful():
        return {"error": "task failed"}, 400
    docx = result.get()
    return send_file(
        io.BytesIO(docx),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name="paper-summary.docx",
    )


if __name__ == "__main__":
    flask_app.run()
