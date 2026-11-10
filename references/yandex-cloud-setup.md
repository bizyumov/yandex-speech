# Account setup and readiness

Run `python scripts/speech_setup.py --check` first. It checks local dependencies, selected settings and credential presence without changing anything. A present key does not prove permission; 403 means denied access, not proof of a particular missing role. Add `--probe-audio ./synthetic.wav` only when actual recognition usage is authorized.

Use the owning profile's existing credential mechanism for `YANDEX_SPEECH_API_KEY`. Never paste a secret into chat, argv or configuration JSON. Service-account SpeechKit requests do not send a folder ID; the old folder setting remains a compatibility field.

## Inspect, plan, then create only when authorized

Management uses either an existing named `yc` profile or `--management-token-env NAME`, where NAME identifies an environment variable containing a management IAM token. It never searches another profile's credentials.

```sh
python scripts/speech_setup.py inspect-account --service-account-id SA_ID --management-profile PROFILE
python scripts/speech_setup.py prepare-key --service-account-id SA_ID --purpose both --management-profile PROFILE
python scripts/speech_setup.py create-key --service-account-id SA_ID --purpose both --management-profile PROFILE
```

Replace SA_ID/PROFILE with the authorized account and existing management profile. `inspect-account` and `prepare-key` only read cloud metadata. Only `create-key` requests a new credential. Purposes are `stt`, `tts`, `both` and separate `s3-static` storage credentials. Use the target owner's required expiry through `--expires-at`; static storage keys do not accept that field.

Without `--secret-out`, prepare/create generate a filename under the invoking user's `~/secrets`: `yandex-speech-<account>-<purpose>-<unique-id>.json`. The resolved filename is returned as `secret_file`. Creation makes missing destination directories. A prepare operation does not create directories/files; to create at its proposed path, pass that returned path explicitly to `create-key --secret-out PATH`. You can also select any other new response file with `--secret-out PATH`. Existing files are never overwritten, including if another writer creates the file during setup. Ordinary filesystem permissions apply: directory ownership, fixed 0700/0600 modes and readable ancestors are not requirements; traversable symlinked parent paths work.

Creation saves the complete one-time response JSON before interpreting optional metadata. Terminal output contains only metadata and the filename. A timeout or failed save requires inspection of account inventory and destination before another attempt; the helper never retries creation, grants roles, rotates or revokes keys automatically. Scope and requested expiry must match the creation response. An unexpected response is retained in the saved file and reported as a failure.

## Apply the saved key to the agent's env

**After successful creation and saving, the agent must call this separate helper with the returned `secret_file` and its own `.env` file:**

```sh
python scripts/speech_setup.py apply-key --secret-file /returned/path/key.json --env-file "$HERMES_HOME/.env"
```

For a host other than Hermes, pass its intended env file explicitly. This command reads the saved response without contacting Yandex or requiring management authentication. It leaves that JSON unchanged. It adds or updates only the credential's variables, removes stale duplicate assignments, preserves unrelated settings/comments and existing env ownership/mode, and prints the env filename and updated variable names without values. Creation and env application remain separate commands; `create-key` does not edit env.

Assignments are placed beneath this exact comment (the requested section name is retained):

```dotenv
# Yandex Search API keys ([https://github.com/bizyumov/yandex-speech/](https://github.com/bizyumov/yandex-speech/))
```

| Variable | Source and behavior |
|---|---|
| `YANDEX_SPEECH_API_KEY` | `secret` in the saved `apiKey` response; used for SpeechKit STT/TTS. |
| `YANDEX_SPEECH_AWS_ACCESS_KEY_ID` | `accessKey.keyId` in a saved static-storage-key response. |
| `YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY` | `secret` in that static-storage-key response. |
| `YANDEX_CLOUD_FOLDER_ID` | Existing shared setting, possibly used by Yandex Search: preserve it unchanged and in place. The key response does not supply a replacement. Service-account SpeechKit calls do not send it. |
| `YANDEX_SPEECH_S3_BUCKET` | Existing selected bucket setting; neither key helper creates or selects a bucket. Preserve it. |
| `YANDEX_SPEECH_S3_ENDPOINT` | Existing storage endpoint setting; preserve it. |
| `YANDEX_SPEECH_S3_PREFIX` | Existing object-prefix setting; preserve it. |

Applying an API key preserves the two storage-key variables; applying a storage key preserves the SpeechKit API key. Bucket/endpoint/prefix come from the selected storage configuration, not the IAM key-creation response.

**Gateway environment variables load at startup. Writing `.env` does not update the running gateway.** Until its next start, the agent must explicitly supply the new variables for immediate speech generation. Read the saved response locally, take `secret` as `YANDEX_SPEECH_API_KEY`, and pass it in the generation subprocess's environment (using `sys.executable` for a Python child), without putting the value in argv, chat or command output. For storage, use the two static-key mappings above. An env change in a terminal child does not update the parent gateway or its built-in speech tool. Do not claim the running gateway adopted a new key merely because `apply-key` succeeded.

## Required authority

- Speech recognition normally needs `ai.speechkit-stt.user` and API-key scope `yc.ai.speechkitStt.execute`; synthesis needs `ai.speechkit-tts.user` and `yc.ai.speechkitTts.execute`.
- Creating API keys needs `iam.serviceAccounts.apiKeyAdmin` or equivalent; storage static keys need `iam.serviceAccounts.accessKeyAdmin` or equivalent. Existing organization policies can also deny creation.
- The upload principal separately needs private-bucket write access; recognition needs object read access. Cleanup is limited to explicitly owned objects. No bucket or public access is created implicitly.

Inspection reports readable key scopes/expiry and direct folder grants separately. Inherited/custom/bucket grants remain unverified when not inspectable; metadata is never labeled successful SpeechKit execution. The resource owner supplies any missing grants; the helper does not silently widen access.

[API keys and scopes](https://yandex.cloud/en/docs/iam/api-ref/ApiKey/create) · [static storage keys](https://yandex.cloud/en/docs/iam/awscompatibility/api-ref/AccessKey/create) · [direct folder bindings](https://yandex.cloud/en/docs/resource-manager/api-ref/Folder/listAccessBindings) · [SpeechKit authentication](https://aistudio.yandex.ru/en/docs/speechkit/concepts/auth).
