"""Deterministic offline tests for generate_model_catalog.py."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FIXTURE_RAW = {
    "deepinfra": {
        "id": "deepinfra",
        "name": "Deep Infra",
        "models": {
            "meta-llama/llama-3-8b-instruct": {
                "id": "meta-llama/llama-3-8b-instruct",
                "limit": {"context": 8192, "input": 8192, "output": 2048},
            }
        },
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "models": {
            "gpt-4o": {
                "id": "gpt-4o",
                "limit": {"context": 128_000, "input": 128_000, "output": 4_096},
            },
            "gpt-4": {
                "id": "gpt-4",
                "limit": {"context": 8_192, "output": 8_192},
            },
        },
    },
    "google": {
        "id": "google",
        "name": "Google",
        "models": {
            "gemini-pro": {
                "id": "gemini-pro",
                "limit": {"context": 1_000_000, "input": 1_000_000, "output": 4_096},
            },
        },
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "models": {
            "claude-3-opus": {
                "id": "claude-3-opus",
                "limit": {"context": 200_000, "input": 200_000, "output": 4_096},
            },
        },
    },
    "openrouter": {
        "id": "openrouter",
        "name": "OpenRouter",
        "models": {
            "meta-llama/llama-3-70b": {
                "id": "meta-llama/llama-3-70b",
                "limit": {"context": 8_192},
            },
        },
    },
    "ollama-cloud": {
        "id": "ollama-cloud",
        "name": "Ollama Cloud",
        "models": {
            "llama3:8b": {
                "id": "llama3:8b",
                "limit": {"context": 8_192, "output": 2_048},
            },
        },
    },
    "xai": {
        "id": "xai",
        "name": "xAI",
        "models": {},
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "models": {
            "deepseek-chat": {
                "id": "deepseek-chat",
                "limit": {"context": 64_000},
            },
        },
    },
    "mistral": {
        "id": "mistral",
        "name": "Mistral",
        "models": {
            "mistral-large": {
                "id": "mistral-large",
                "limit": {"context": 128_000, "output": 8_192},
            },
        },
    },
    "groq": {
        "id": "groq",
        "name": "Groq",
        "models": {
            "llama-3-8b": {
                "id": "llama-3-8b",
                "limit": {"context": 8_192, "output": 8_192},
            },
        },
    },
    "azure": {
        "id": "azure",
        "name": "Azure",
        "models": {
            "gpt-4": {
                "id": "gpt-4",
                "limit": {"context": 8_192},
            },
        },
    },
}

EXPECTED_PROVIDERS = {
    "openai": {
        "gpt-4o": {"contextWindow": 128_000, "inputLimit": 128_000, "outputLimit": 4_096},
        "gpt-4": {"contextWindow": 8_192, "outputLimit": 8_192},
    },
    "azure-openai": {
        "gpt-4": {"contextWindow": 8_192},
    },
    "gemini": {
        "gemini-pro": {"contextWindow": 1_000_000, "inputLimit": 1_000_000, "outputLimit": 4_096},
    },
    "anthropic": {
        "claude-3-opus": {"contextWindow": 200_000, "inputLimit": 200_000, "outputLimit": 4_096},
    },
    "openrouter": {
        "meta-llama/llama-3-70b": {"contextWindow": 8_192},
    },
    "ollama-cloud": {
        "llama3:8b": {"contextWindow": 8_192, "outputLimit": 2_048},
    },
    # xai is omitted because compact skips providers with no selected models
    "deepseek": {
        "deepseek-chat": {"contextWindow": 64_000},
    },
    "mistral": {
        "mistral-large": {"contextWindow": 128_000, "outputLimit": 8_192},
    },
    "groq": {
        "llama-3-8b": {"contextWindow": 8_192, "outputLimit": 8_192},
    },
}


def _write_fixture(tmp_path: Path, fixture: dict) -> Path:
    """Write fixture JSON and return the file path."""
    p = tmp_path / "fixture_raw.json"
    p.write_text(json.dumps(fixture, separators=(",", ":")))
    return p


def _run_generator(tmp_path: Path, args: list[str] | None = None) -> Path:
    """Run the generator script and return the output path."""
    output = tmp_path / "catalog.json"
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent.parent / "scripts" / "generate_model_catalog.py"),
        "--output", str(output),
    ]
    if args:
        cmd.extend(args)
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output


class TestGeneratorDefaultSource:
    """Test that DEFAULT_SOURCE points to models.dev API."""

    def test_default_source_is_models_dev(self):
        from scripts.generate_model_catalog import DEFAULT_SOURCE
        assert DEFAULT_SOURCE == "https://models.dev/api.json"

    def test_no_opencode_reference_in_default(self):
        from scripts.generate_model_catalog import DEFAULT_SOURCE
        assert "opencode" not in DEFAULT_SOURCE.lower()
        assert "raw.githubusercontent.com" not in DEFAULT_SOURCE


class TestGeneratorOfflineMode:
    """Test offline generation with local fixture file."""

    def test_offline_generation_produces_valid_catalog(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        assert catalog["schemaVersion"] == 1
        assert catalog["sourceUrl"] == "https://models.dev/api.json"
        assert "sourceSha256" in catalog
        assert "retrievedAt" in catalog
        assert "sourceRevision" not in catalog  # not provided

    def test_offline_providers_are_compacted_correctly(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        providers = catalog["providers"]
        for provider, expected in EXPECTED_PROVIDERS.items():
            assert provider in providers, f"Provider {provider} missing"
            assert providers[provider] == expected

    def test_offline_google_maps_to_gemini(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        assert "gemini" in catalog["providers"]
        assert "google" not in catalog["providers"]
        assert "gemini-pro" in catalog["providers"]["gemini"]

    def test_offline_azure_maps_to_azure_openai(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        assert "azure-openai" in catalog["providers"]
        assert "azure" not in catalog["providers"]

    def test_source_revision_absent_when_not_provided(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        assert "sourceRevision" not in catalog


class TestGeneratorSourceRevision:
    """Test sourceRevision is optional and only included when provided."""

    def test_source_revision_included_when_provided(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(
            tmp_path,
            ["--source", str(fixture), "--source-revision", "abc123"],
        )
        catalog = json.loads(output.read_text())

        assert catalog["sourceRevision"] == "abc123"

    def test_source_revision_absent_when_none(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(
            tmp_path,
            ["--source", str(fixture), "--source-revision", ""],
        )
        catalog = json.loads(output.read_text())

        # Empty string is truthy for argparse, so it gets written
        assert catalog["sourceRevision"] == ""


class TestGeneratorCompactFunction:
    """Test the compact() function directly with mocked data."""

    def test_compact_filters_invalid_types(self):
        from scripts.generate_model_catalog import compact

        source = {
            "openai": {
                "models": {
                    "valid-model": {"limit": {"context": 100}},
                    123: {"limit": {"context": 200}},  # non-string key
                    "invalid-model": "not-a-dict",  # non-dict value
                    "bool-model": {"limit": {"context": True}},  # bool, not int
                    "neg-model": {"limit": {"context": -1}},  # negative
                    "big-model": {"limit": {"context": 999_999_999_999}},  # too big
                }
            }
        }
        result = compact(source)
        assert "valid-model" in result["openai"]
        assert 123 not in result["openai"]
        assert "invalid-model" not in result["openai"]
        assert "bool-model" not in result["openai"]
        assert "neg-model" not in result["openai"]
        assert "big-model" not in result["openai"]

    def test_compact_prefers_limit_over_model_level_fields(self):
        from scripts.generate_model_catalog import compact

        source = {
            "openai": {
                "models": {
                    "gpt-test": {
                        "limit": {"context": 10, "input": 20, "output": 30},
                        "contextWindow": 999,  # should be ignored
                        "inputLimit": 999,  # should be ignored
                    }
                }
            }
        }
        result = compact(source)
        assert result["openai"]["gpt-test"] == {
            "contextWindow": 10,
            "inputLimit": 20,
            "outputLimit": 30,
        }

    def test_compact_empty_provider_is_omitted(self):
        from scripts.generate_model_catalog import compact

        source = {
            "openai": {"models": {}},
            "anthropic": {},
            "google": {"models": {"gemini": {"limit": {"context": 100}}}},
        }
        result = compact(source)
        assert "openai" not in result
        assert "anthropic" not in result
        assert "gemini" in result["gemini"]


class TestGeneratorByteLimits:
    """Test size validation."""

    def test_large_source_raises(self, tmp_path: Path):
        import subprocess as _subprocess

        big_file = tmp_path / "big.json"
        big_file.write_text('{"openai": {"models": {"x": {"limit": {"context": 100}}}}}' * 2_000_000)
        output = tmp_path / "catalog.json"
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "scripts" / "generate_model_catalog.py"),
            "--output", str(output),
            "--source", str(big_file),
        ]
        result = _subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode != 0
        assert "exceeds byte limit" in result.stderr

    def test_too_many_entries_raises(self, tmp_path: Path):
        import subprocess as _subprocess

        # MAX_ENTRIES=10000; compact raises before size check
        huge_models = {}
        for i in range(10_001):
            huge_models[f"model-{i}"] = {"limit": {"context": 100}}
        fixture = {
            "openai": {"id": "openai", "models": huge_models},
        }
        fixture_file = tmp_path / "huge.json"
        fixture_file.write_text(json.dumps(fixture, separators=(",", ":")))
        output = tmp_path / "catalog.json"
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "scripts" / "generate_model_catalog.py"),
            "--output", str(output),
            "--source", str(fixture_file),
        ]
        result = _subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode != 0
        assert "too many selected entries" in result.stderr


class TestGeneratorProviderMapping:
    """Test the PROVIDERS mapping is correct."""

    def test_provider_aliases(self):
        from scripts.generate_model_catalog import PROVIDERS

        assert PROVIDERS["openai"] == "openai"
        assert PROVIDERS["azure"] == "azure-openai"
        assert PROVIDERS["google"] == "gemini"
        assert PROVIDERS["openrouter"] == "openrouter"
        assert PROVIDERS["ollama-cloud"] == "ollama-cloud"
        assert PROVIDERS["anthropic"] == "anthropic"
        assert PROVIDERS["xai"] == "xai"
        assert PROVIDERS["deepseek"] == "deepseek"
        assert PROVIDERS["mistral"] == "mistral"
        assert PROVIDERS["groq"] == "groq"

    def test_no_duplicate_local_names(self):
        from scripts.generate_model_catalog import PROVIDERS

        local_names = list(PROVIDERS.values())
        assert len(local_names) == len(set(local_names)), "Duplicate local provider names"


class TestGeneratorContextAndCompaction:
    """Test generator output preserves compaction context, input, output fields."""

    def test_compact_preserves_all_three_fields(self, tmp_path: Path):
        fixture = {
            "openai": {
                "models": {
                    "full-model": {
                        "limit": {"context": 100_000, "input": 80_000, "output": 20_000},
                    }
                }
            }
        }
        fixture_file = _write_fixture(tmp_path, fixture)
        output = _run_generator(tmp_path, ["--source", str(fixture_file)])
        catalog = json.loads(output.read_text())

        fields = catalog["providers"]["openai"]["full-model"]
        assert fields == {
            "contextWindow": 100_000,
            "inputLimit": 80_000,
            "outputLimit": 20_000,
        }

    def test_compact_partial_fields(self, tmp_path: Path):
        fixture = {
            "openai": {
                "models": {
                    "context-only": {"limit": {"context": 50_000}},
                    "input-only": {"limit": {"input": 40_000}},
                    "output-only": {"limit": {"output": 10_000}},
                }
            }
        }
        fixture_file = _write_fixture(tmp_path, fixture)
        output = _run_generator(tmp_path, ["--source", str(fixture_file)])
        catalog = json.loads(output.read_text())

        assert catalog["providers"]["openai"]["context-only"] == {"contextWindow": 50_000}
        assert catalog["providers"]["openai"]["input-only"] == {"inputLimit": 40_000}
        assert catalog["providers"]["openai"]["output-only"] == {"outputLimit": 10_000}


class TestGeneratorSourceUrlInOutput:
    """Verify sourceUrl in output matches expected models.dev endpoint."""

    def test_source_url_is_models_dev_api(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        assert catalog["sourceUrl"] == "https://models.dev/api.json"
        assert "github" not in catalog["sourceUrl"]
        assert "opencode" not in catalog["sourceUrl"]

    def test_no_opencode_references_in_output(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog_text = output.read_text()

        assert "opencode" not in catalog_text.lower()
        assert "raw.githubusercontent.com" not in catalog_text


class TestGeneratorDeterministicMetadata:
    """Test that metadata fields are deterministic given the same input."""

    def test_schema_version_is_one(self, tmp_path: Path):
        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        assert catalog["schemaVersion"] == 1

    def test_sha256_matches_input(self, tmp_path: Path):
        import hashlib

        fixture_file = _write_fixture(tmp_path, FIXTURE_RAW)
        raw = fixture_file.read_bytes()
        expected_sha = hashlib.sha256(raw).hexdigest()

        output = _run_generator(tmp_path, ["--source", str(fixture_file)])
        catalog = json.loads(output.read_text())

        assert catalog["sourceSha256"] == expected_sha

    def test_retrieved_at_is_iso_format(self, tmp_path: Path):
        import re

        fixture = _write_fixture(tmp_path, FIXTURE_RAW)
        output = _run_generator(tmp_path, ["--source", str(fixture)])
        catalog = json.loads(output.read_text())

        # Should be ISO 8601 format ending with Z
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", catalog["retrievedAt"])
