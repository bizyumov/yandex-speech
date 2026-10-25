# Media preparation

Source admission follows decoder capability, not a filename allow-list. FLAC, AAC/M4A, AMR, WebM/Opus, MP3, video audio, multichannel and float/24-bit WAV are accepted when installed FFmpeg/ffprobe can decode them. The skill does not fetch arbitrary source URLs or bypass protected media. Supply an authorized local source.

The first audio stream is selected deterministically; --audio-track N selects a zero-based audio-stream index (not the container's global stream index). Probe actual headers even when the suffix is misleading. Preserve original path/hash, selected track, representation, duration and timestamp origin. Provider-relative times are mapped once back to the original selected audio timeline. No neighboring derived file may overwrite the source.

For recording recognition, compatible mono WAV/LPCM/Ogg/MP3 may be preserved; other input is extracted, decoded, downmixed and resampled to mono 16 kHz signed-16-bit little-endian PCM/WAV. PCM rates outside the provider-supported 8/16/48 kHz are converted. The source format can be broader than the prepared request format.

Headerless input uses --audio-format lpcm --sample-format s16le --sample-rate 16000 --channels 1 (replace these with its actual source values). Other described integer/float sample formats are converted. A partial final sample frame is invalid. A WAV header is not raw PCM.

Arriving audio uses owned source/decoder buffers; native host 16 kHz mono s16le bypasses conversion. No more than 1,048,576 source bytes or 64,000 prepared PCM bytes queue locally. Overflow fails rather than dropping speech. Decoder/resampler flush must finish successfully before the one provider EOU. A corrupt/truncated input, missing decoder, missing raw metadata or missing audio track fails explicitly. A source requiring seekable input should use recorded recognition; this Phase 1 stream does not claim a successful buffered fallback.

[SpeechKit formats](https://aistudio.yandex.ru/en/docs/speechkit/formats) · [v3 audio request](https://aistudio.yandex.ru/en/docs/speechkit/stt-v3/api-ref/grpc/AsyncRecognizer/recognizeFile).
