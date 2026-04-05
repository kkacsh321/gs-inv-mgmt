import unittest
from unittest.mock import Mock, patch

from app.services import voice_runtime
from app.services.voice_runtime import VoiceRuntimeConfig


class VoiceRuntimeTests(unittest.TestCase):
    def test_safe_int_and_helpers(self) -> None:
        self.assertEqual(voice_runtime._safe_int("7", 1), 7)
        self.assertEqual(voice_runtime._safe_int("bad", 3), 3)
        self.assertEqual(voice_runtime._auth_headers(VoiceRuntimeConfig(
            enabled=True,
            stt_enabled=True,
            tts_enabled=True,
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="k",
            timeout_seconds=30,
            stt_model="m",
            stt_language="",
            tts_model="m2",
            tts_voice="alloy",
            tts_response_format="mp3",
            tts_max_chars=1000,
        )), {"Authorization": "Bearer k"})
        self.assertEqual(voice_runtime._auth_headers(VoiceRuntimeConfig(
            enabled=True,
            stt_enabled=True,
            tts_enabled=True,
            provider="localai",
            base_url="http://localai:8080/v1",
            api_key="",
            timeout_seconds=30,
            stt_model="m",
            stt_language="",
            tts_model="m2",
            tts_voice="alloy",
            tts_response_format="mp3",
            tts_max_chars=1000,
        )), {})

        self.assertEqual(
            voice_runtime._endpoint_candidates("https://api.openai.com/v1", "audio/speech"),
            ["https://api.openai.com/v1/audio/speech", "https://api.openai.com/audio/speech"],
        )
        self.assertEqual(
            voice_runtime._endpoint_candidates("http://localai:8080", "audio/transcriptions"),
            ["http://localai:8080/audio/transcriptions", "http://localai:8080/v1/audio/transcriptions"],
        )
        self.assertEqual(voice_runtime._endpoint_candidates("", "audio/speech"), [])

    @patch("app.services.voice_runtime.get_runtime_str")
    @patch("app.services.voice_runtime.get_runtime_int")
    @patch("app.services.voice_runtime.get_runtime_bool")
    def test_resolve_voice_runtime_config(self, mock_bool, mock_int, mock_str) -> None:
        bool_map = {
            "ai_voice_enabled": True,
            "ai_voice_stt_enabled": True,
            "ai_voice_tts_enabled": True,
        }
        int_map = {
            "ai_voice_timeout_seconds": 50,
            "ai_voice_tts_max_chars": 2200,
        }
        str_map = {
            "ai_voice_provider": "localai",
            "ai_voice_base_url": "http://localai:8080/v1/",
            "ai_voice_api_key": "",
            "ai_voice_stt_model": "whisper",
            "ai_voice_stt_language": "en",
            "ai_voice_tts_model": "tts-model",
            "ai_voice_tts_voice": "alloy",
            "ai_voice_tts_response_format": "wav",
        }
        mock_bool.side_effect = lambda repo, key, default: bool_map.get(key, default)
        mock_int.side_effect = lambda repo, key, default: int_map.get(key, default)
        mock_str.side_effect = lambda repo, key, default: str_map.get(key, default)

        cfg = voice_runtime.resolve_voice_runtime_config(repo=object())
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.provider, "localai")
        self.assertEqual(cfg.base_url, "http://localai:8080/v1")
        self.assertEqual(cfg.timeout_seconds, 50)
        self.assertEqual(cfg.tts_max_chars, 2200)
        self.assertEqual(cfg.tts_response_format, "wav")

    def _cfg(self, **overrides) -> VoiceRuntimeConfig:
        base = dict(
            enabled=True,
            stt_enabled=True,
            tts_enabled=True,
            provider="localai",
            base_url="http://localai:8080/v1",
            api_key="",
            timeout_seconds=15,
            stt_model="whisper-1",
            stt_language="",
            tts_model="tts-1",
            tts_voice="alloy",
            tts_response_format="mp3",
            tts_max_chars=100,
        )
        base.update(overrides)
        return VoiceRuntimeConfig(**base)

    def test_transcribe_audio_guards_and_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            voice_runtime.transcribe_audio_bytes(self._cfg(enabled=False), audio_bytes=b"a")
        with self.assertRaisesRegex(RuntimeError, "base URL"):
            voice_runtime.transcribe_audio_bytes(self._cfg(base_url=""), audio_bytes=b"a")
        with self.assertRaisesRegex(RuntimeError, "STT model"):
            voice_runtime.transcribe_audio_bytes(self._cfg(stt_model=""), audio_bytes=b"a")
        with self.assertRaisesRegex(RuntimeError, "requires api key"):
            voice_runtime.transcribe_audio_bytes(
                self._cfg(provider="openai", api_key=""),
                audio_bytes=b"a",
            )

        good_resp = Mock()
        good_resp.raise_for_status.return_value = None
        good_resp.json.return_value = {"text": "hello world"}
        with patch("app.services.voice_runtime.requests.post", return_value=good_resp):
            text = voice_runtime.transcribe_audio_bytes(self._cfg(), audio_bytes=b"a")
            self.assertEqual(text, "hello world")

        bad_resp = Mock()
        bad_resp.raise_for_status.return_value = None
        bad_resp.json.return_value = {}
        with patch("app.services.voice_runtime.requests.post", return_value=bad_resp):
            with self.assertRaisesRegex(RuntimeError, "across all endpoint variants"):
                voice_runtime.transcribe_audio_bytes(self._cfg(), audio_bytes=b"a")

        with patch("app.services.voice_runtime._endpoint_candidates", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "no endpoint candidates"):
                voice_runtime.transcribe_audio_bytes(self._cfg(), audio_bytes=b"a")

    def test_synthesize_speech_guards_and_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            voice_runtime.synthesize_speech_bytes(self._cfg(tts_enabled=False), text="hello")
        with self.assertRaisesRegex(RuntimeError, "base URL"):
            voice_runtime.synthesize_speech_bytes(self._cfg(base_url=""), text="hello")
        with self.assertRaisesRegex(RuntimeError, "TTS model"):
            voice_runtime.synthesize_speech_bytes(self._cfg(tts_model=""), text="hello")
        with self.assertRaisesRegex(RuntimeError, "requires api key"):
            voice_runtime.synthesize_speech_bytes(self._cfg(provider="openai", api_key=""), text="hello")
        with self.assertRaisesRegex(RuntimeError, "No text supplied"):
            voice_runtime.synthesize_speech_bytes(self._cfg(), text="  ")

        wav_cfg = self._cfg(tts_response_format="wav", tts_max_chars=5)
        resp = Mock()
        resp.raise_for_status.return_value = None
        resp.content = b"audio-bytes"
        with patch("app.services.voice_runtime.requests.post", return_value=resp):
            data, mime = voice_runtime.synthesize_speech_bytes(wav_cfg, text="hello world")
            self.assertEqual(data, b"audio-bytes")
            self.assertEqual(mime, "audio/wav")

        with patch("app.services.voice_runtime.requests.post", side_effect=Exception("boom")):
            with self.assertRaisesRegex(RuntimeError, "across all endpoint variants"):
                voice_runtime.synthesize_speech_bytes(self._cfg(), text="hello")

        with patch("app.services.voice_runtime._endpoint_candidates", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "no endpoint candidates"):
                voice_runtime.synthesize_speech_bytes(self._cfg(), text="hello")


if __name__ == "__main__":
    unittest.main()
