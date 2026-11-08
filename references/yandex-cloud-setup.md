# Account setup and readiness

Run `python scripts/speech_setup.py --check` first. It checks local dependencies, selected settings and credential presence without changing anything. A present key does not prove permission; 403 means denied access, not proof of a particular missing role. Add `--probe-audio ./synthetic.wav` only when actual recognition usage is authorized.

Use the owning profile's existing credential mechanism for `YANDEX_SPEECH_API_KEY`. Never paste a secret into chat, argv or configuration JSON. Service-account SpeechKit requests do not send a folder ID; the old folder setting remains a compatibility field.

## Inspect, plan, then create only when authorized

Management uses either an existing named `yc` profile or `--management-token-env NAME`, where NAME identifies an environment variable containing a management IAM token. It never searches another profile's credentials.

```sh
python scripts/speech_setup.py inspect-account --service-account-id SA_ID --management-profile PROFILE
mkdir -m 700 /PRIVATE/new-speech-keys
python scripts/speech_setup.py prepare-key --service-account-id SA_ID --purpose both --secret-out /PRIVATE/new-speech-keys/new-key.json --management-profile PROFILE
python scripts/speech_setup.py create-key --service-account-id SA_ID --purpose both --secret-out /PRIVATE/new-speech-keys/new-key.json --management-profile PROFILE
```

Replace SA_ID/PROFILE with the explicitly authorized target/profile and /PRIVATE with an existing protected location outside the skill checkout. Use the intended runtime secret store; a shared source directory is not a secret destination. `inspect-account` and `prepare-key` only read metadata. Only `create-key` requests a new credential. Purposes are `stt`, `tts`, `both` and separate `s3-static` storage credentials. Use the target owner's required expiry through `--expires-at`; omission is not approval to ignore a governed expiry policy. Static storage keys do not accept that field.

The secret destination must be absent in an existing owned 0700 directory without symlink redirection. Creation reserves a new 0600 file before requesting the key and writes the one-time response there. Terminal output contains only allowlisted metadata. Existing keys and files are preserved. A timeout or failed secret save requires inspection of the account inventory and destination before another attempt; the helper never retries creation, grants roles, rotates or revokes keys automatically.

The agent imports the protected new credential through the intended runtime's supported secret writer and verifies actual method execution before retiring any old credential. Scope and requested expiry must match the creation response. An unexpected response is retained in the protected file and reported as a failure.

## Required authority

- Speech recognition normally needs `ai.speechkit-stt.user` and API-key scope `yc.ai.speechkitStt.execute`; synthesis needs `ai.speechkit-tts.user` and `yc.ai.speechkitTts.execute`.
- Creating API keys needs `iam.serviceAccounts.apiKeyAdmin` or equivalent; storage static keys need `iam.serviceAccounts.accessKeyAdmin` or equivalent. Existing organization policies can also deny creation.
- The upload principal separately needs private-bucket write access; recognition needs object read access. Cleanup is limited to explicitly owned objects. No bucket or public access is created implicitly.

Inspection reports readable key scopes/expiry and direct folder grants separately. Inherited/custom/bucket grants remain unverified when not inspectable; metadata is never labeled successful SpeechKit execution. The resource owner supplies any missing grants; the helper does not silently widen access.

[API keys and scopes](https://yandex.cloud/en/docs/iam/api-ref/ApiKey/create) · [static storage keys](https://yandex.cloud/en/docs/iam/awscompatibility/api-ref/AccessKey/create) · [direct folder bindings](https://yandex.cloud/en/docs/resource-manager/api-ref/Folder/listAccessBindings) · [SpeechKit authentication](https://aistudio.yandex.ru/en/docs/speechkit/concepts/auth).
