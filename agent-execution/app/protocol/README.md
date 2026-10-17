# Server-request response schemas

These files are generated from the CLI bundled with `openai-codex==0.162.1`:

```sh
codex app-server generate-json-schema --experimental --out /tmp/codex-protocol
```

Only the five supported server-request response schemas are checked in. They
validate caller responses before anything is written to the runtime. Update them
together with the SDK pin and run the transport and interaction tests.

Protocol reference: https://learn.chatgpt.com/docs/app-server#approvals
