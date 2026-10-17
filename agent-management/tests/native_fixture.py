"""Isolated native CodexServer process for the hub integration test."""

import json
import os
import sys
from pathlib import Path

source, root, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, str(source))
os.chdir(root)

import uvicorn  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.mock_provider import provider  # noqa: E402

calls = 0


def question_once(request, final_item):
    global calls
    calls += 1
    if calls != 1:
        return final_item
    return {
        "id": "fc_question",
        "type": "function_call",
        "call_id": "question-call",
        "name": "request_user_input",
        "status": "completed",
        "arguments": json.dumps(
            {
                "questions": [
                    {
                        "id": "strategy",
                        "header": "Choose",
                        "question": "Which strategy?",
                        "options": [
                            {"label": "A", "description": "First"},
                            {"label": "B", "description": "Second"},
                        ],
                    }
                ]
            }
        ),
    }


with provider(response_factory=question_once) as base_url:
    home = root / "codex"
    home.mkdir()
    (home / "config.toml").write_text(
        'model_provider = "local_test"\n[model_providers.local_test]\n'
        f'name = "Local fixture"\nbase_url = "{base_url}"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
    )
    settings = Settings(
        _env_file=None,
        api_token="native-integration-test-token-12345",
        data_dir=root / "data",
        workspace_root=root / "workspaces",
        codex_home=home,
        codex_model="mock-codex",
        run_timeout_seconds=20,
        shutdown_timeout_seconds=1,
    )
    uvicorn.run(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
