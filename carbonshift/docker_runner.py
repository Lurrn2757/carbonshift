"""Docker CLI adapter. Commands are argument arrays; workloads are allowlisted."""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime

IMAGE = "carbonshift-workload:0.4"


class RunnerError(RuntimeError):
    pass


@dataclass
class Container:
    id: str
    status: str
    labels: dict
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    oom_killed: bool = False


def timestamp(value):
    if not value or value.startswith("0001-"):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class DockerRunner:
    def command(self, args, timeout=15, include_stderr=False):
        try:
            result = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunnerError(f"Docker command unavailable or timed out: {exc}") from exc
        if result.returncode:
            raise RunnerError((result.stderr or result.stdout or "Docker command failed")[-2000:])
        return (result.stdout + result.stderr if include_stderr else result.stdout).strip()

    def check(self, require_image=True):
        version = self.command(["version", "--format", "{{.Server.Version}}"])
        image_id = self.command(["image", "inspect", IMAGE, "--format", "{{.Id}}"] ) if require_image else None
        return {"docker_server": version, "workload_image": IMAGE, "image_id": image_id}

    def inspect(self, name):
        # A successful empty listing means absent. Daemon failures raise; they
        # must never be interpreted as permission to create another container.
        found = self.command(["container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"])
        if not found:
            return None
        try:
            data = json.loads(self.command(["container", "inspect", name]))[0]
            state = data["State"]
            return Container(
                id=data["Id"], status=state["Status"], labels=data["Config"].get("Labels") or {},
                started_at=timestamp(state.get("StartedAt")), finished_at=timestamp(state.get("FinishedAt")),
                exit_code=state.get("ExitCode"), oom_killed=bool(state.get("OOMKilled")),
            )
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise RunnerError("Malformed Docker inspect response.") from exc

    def create(self, job, queue_id):
        spec = job["snapshot"]["execution"]
        if spec["workload"] not in ("checksum-demo", "image-batch"):
            raise RunnerError("Workload is not allowlisted.")
        mounts=[]; command=["--seconds",str(spec["run_seconds"])]; memory="128m"
        if spec["workload"] == "image-batch":
            from .assets import validate_bundle, output_directory
            try:
                directory,_=validate_bundle(spec["image_batch"])
                output=output_directory(job["id"])
                if "," in str(directory) or "," in str(output):
                    raise ValueError("Artifact paths must not contain commas.")
            except (OSError,ValueError,KeyError) as exc:
                raise RunnerError(f"Image inputs unavailable or changed: {exc}") from exc
            mounts=["--mount",f"type=bind,src={directory},dst=/input,readonly",
                    "--mount",f"type=bind,src={output},dst=/output",
                    "--tmpfs","/tmp:rw,noexec,nosuid,size=16m", "--entrypoint","python"]
            image=spec["image_batch"]
            command=["-u","/work/images.py","--max-edge",str(image["max_edge"]),"--quality",str(image["quality"]),
                     "--manifest-sha256",image["manifest_sha256"],"--timeout",str(spec["timeout_seconds"])]
            memory="512m"
        return self.command([
            "container", "create", "--pull", "never", "--name", job["container_name"],
            "--label", f"carbonshift.queue={queue_id}", "--label", f"carbonshift.job={job['id']}",
            "--network", "none", "--read-only", "--user", "65534:65534",
            "--cpus", "1", "--memory", memory, "--memory-swap", memory, "--pids-limit", "64",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
            "--log-opt", "compress=false",
            "--restart", "no", *mounts, IMAGE, *command,
        ])

    def start(self, container_id):
        self.command(["container", "start", container_id])

    def stop(self, container_id):
        self.command(["container", "stop", "--time", "2", container_id])

    def logs(self, container_id):
        return self.command(["container", "logs", "--tail", "200", container_id], include_stderr=True)[-65536:]
