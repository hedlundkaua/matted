import os
import subprocess
import tempfile
import threading
from typing import Dict, List # Adicionado para compatibilidade
from .base import LLMProvider


class CodexProvider(LLMProvider):
    """
    Provider for the local `codex` CLI tool.
    Encapsulates the subprocess logic to interact with the binary.
    """

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.default_sandbox = "workspace-write"
        self.default_approval = "on-request"
        self.default_verbose = "0"
        self.default_stream = "filtered"

    def _verbose_codex_output(self) -> bool:
        raw = os.environ.get("MATTED_CODEX_VERBOSE", self.default_verbose)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _codex_stream_mode(self) -> str:
        raw = os.environ.get("MATTED_CODEX_STREAM", self.default_stream).strip().lower()
        return raw if raw in {"filtered", "quiet"} else self.default_stream

    def _should_print_codex_line(self, line: str, *, stream_name: str, state: Dict[str, bool]) -> bool:
        stripped = line.strip()
        if not stripped:
            return False

        lowered = stripped.lower()
        if stream_name == "stderr":
            return not lowered.startswith("warning: codex could not find bubblewrap")

        if stripped == "user":
            state["suppress_prompt"] = True
            return False
        if stripped == "codex":
            state["suppress_prompt"] = False
            return False
        if stripped == "exec":
            state["suppress_prompt"] = False
            state["expect_exec_cmd"] = True
            return False

        if state.get("suppress_prompt"):
            return False

        noisy_prefixes = (
            "OpenAI Codex", "--------", "workdir:", "model:", "provider:",
            "approval:", "sandbox:", "reasoning effort:", "reasoning summaries:", "session id:",
        )
        if stripped.startswith(noisy_prefixes):
            return False
        if lowered.startswith("warning: codex could not find bubblewrap"):
            return False

        return True

    def _format_codex_line(self, line: str, state: Dict[str, bool], agent_name: str = "System") -> str:
        stripped = line.rstrip()
        if state.get("expect_exec_cmd"):
            state["expect_exec_cmd"] = False
            return f"$ {stripped}"
        return stripped

    def _codex_cli_cmd(self, output_last_message_path: str) -> List[str]:
        sandbox = os.environ.get("MATTED_CODEX_SANDBOX", self.default_sandbox)
        approval = os.environ.get("MATTED_CODEX_APPROVAL", self.default_approval)
        return [
            "codex",
            "--sandbox",
            sandbox,
            "--ask-for-approval",
            approval,
            "exec",
            "--skip-git-repo-check",
            "--cd",
            self.root_dir,
            "--output-last-message",
            output_last_message_path,
            "-",
        ]

    def generate(self, prompt: str) -> str:
        # We use a temporary file to capture the last message as per original logic
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=True) as output_file:
            cmd = self._codex_cli_cmd(output_file.name)

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.root_dir,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            verbose = self._verbose_codex_output()
            stream_mode = self._codex_stream_mode()
            stream_state = {"suppress_prompt": False, "expect_exec_cmd": False}

            def stream_pipe(pipe, sink, stream_name):
                try:
                    while True:
                        line = pipe.readline()
                        if not line:
                            break
                        if verbose:
                            print(line, end="", flush=True)
                        elif stream_mode == "filtered" and self._should_print_codex_line(line, stream_name=stream_name, state=stream_state):
                            # Note: agent_name is generic here; fine for general provider usage
                            print(self._format_codex_line(line, stream_state), flush=True)
                        sink.append(line)
                finally:
                    pipe.close()

            stdout_thread = threading.Thread(target=stream_pipe, args=(process.stdout, stdout_lines, "stdout"), daemon=True)
            stderr_thread = threading.Thread(target=stream_pipe, args=(process.stderr, stderr_lines, "stderr"), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()

            return_code = process.wait()
            stdout_thread.join()
            stderr_thread.join()

            output_completo = "".join(stdout_lines)
            stderr_completo = "".join(stderr_lines)

            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, cmd, output=output_completo, stderr=stderr_completo)

            output_file.seek(0)
            last_message = output_file.read().strip()
            return last_message or output_completo.strip()
