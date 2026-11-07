# Speech inference compatibility

The application uses the ASR-only and common-only dependencies from NeMo Speech revision `218d0b443441b7d8aa1c832f47df310f186cf0a4`. Its complete inference environment is locked in `speech-requirements.lock.txt`. This permits the security fixes in Lightning 2.6.6 without installing NeMo's training extras, which require an older Lightning release.

Two source compatibility changes are required by this combination:

- Match the optional `weights_only` type annotation in the logger's `save_checkpoint` override to Lightning. The default value and method body stay the same.
- Defer the optional Neptune training logger import until that training integration is requested. This application does not support training or Neptune logging.

`scripts/speech_compat.py` checks the full original file hash, applies only these changes, and checks the resulting hash. It accepts an already patched file only when its hash matches. An unexpected source file stops installation. These are maintained compatibility changes, not an upstream support claim. Remove them after a compatible upstream release passes the speech regression checks.

The source projects retain their licenses: [NVIDIA NeMo Speech](https://github.com/NVIDIA-NeMo/Speech/tree/218d0b443441b7d8aa1c832f47df310f186cf0a4) and [NVIDIA One Logger](https://github.com/NVIDIA/nv-one-logger). The repository does not redistribute their full source. Package advisory scans do not replace review of these changes or the pinned model code.
