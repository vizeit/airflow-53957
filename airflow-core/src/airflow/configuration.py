# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import contextlib
import datetime
import functools
import itertools
import json
import logging
import multiprocessing
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import warnings
from base64 import b64encode
from collections.abc import Generator, Iterable
from configparser import ConfigParser, NoOptionError, NoSectionError
from contextlib import contextmanager
from copy import deepcopy
from io import StringIO
from json.decoder import JSONDecodeError
from re import Pattern
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import urlsplit

from packaging.version import parse as parse_version
from typing_extensions import overload

from airflow.exceptions import AirflowConfigException
from airflow.secrets import DEFAULT_SECRETS_SEARCH_PATH
from airflow.utils import yaml
from airflow.utils.module_loading import import_string
from airflow.utils.weight_rule import WeightRule

if TYPE_CHECKING:
    from airflow.api_fastapi.auth.managers.base_auth_manager import BaseAuthManager
    from airflow.secrets import BaseSecretsBackend

log = logging.getLogger(__name__)

# show Airflow's deprecation warnings
if not sys.warnoptions:
    warnings.filterwarnings(action="default", category=DeprecationWarning, module="airflow")
    warnings.filterwarnings(action="default", category=PendingDeprecationWarning, module="airflow")

_SQLITE3_VERSION_PATTERN = re.compile(r"(?P<version>^\d+(?:\.\d+)*)\D?.*$")

ConfigType = str | int | float | bool
ConfigOptionsDictType = dict[str, ConfigType]
ConfigSectionSourcesType = dict[str, str | tuple[str, str]]
ConfigSourcesType = dict[str, ConfigSectionSourcesType]

ENV_VAR_PREFIX = "AIRFLOW__"


class ConfigModifications:
    """
    Holds modifications to be applied when writing out the config.

    :param rename: Mapping from (old_section, old_option) to (new_section, new_option)
    :param remove: Set of (section, option) to remove
    :param default_updates: Mapping from (section, option) to new default value
    """

    def __init__(self) -> None:
        self.rename: dict[tuple[str, str], tuple[str, str]] = {}
        self.remove: set[tuple[str, str]] = set()
        self.default_updates: dict[tuple[str, str], str] = {}

    def add_rename(self, old_section: str, old_option: str, new_section: str, new_option: str) -> None:
        self.rename[(old_section, old_option)] = (new_section, new_option)

    def add_remove(self, section: str, option: str) -> None:
        self.remove.add((section, option))

    def add_default_update(self, section: str, option: str, new_default: str) -> None:
        self.default_updates[(section, option)] = new_default


def _parse_sqlite_version(s: str) -> tuple[int, ...]:
    match = _SQLITE3_VERSION_PATTERN.match(s)
    if match is None:
        return ()
    return tuple(int(p) for p in match.group("version").split("."))


@overload
def expand_env_var(env_var: None) -> None: ...


@overload
def expand_env_var(env_var: str) -> str: ...


def expand_env_var(env_var: str | None) -> str | None:
    """
    Expand (potentially nested) env vars.

    Repeat and apply `expandvars` and `expanduser` until
    interpolation stops having any effect.
    """
    if not env_var or not isinstance(env_var, str):
        return env_var
    while True:
        interpolated = os.path.expanduser(os.path.expandvars(str(env_var)))
        if interpolated == env_var:
            return interpolated
        env_var = interpolated


def run_command(command: str) -> str:
    """Run command and returns stdout."""
    process = subprocess.Popen(
        shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True
    )
    output, stderr = (stream.decode(sys.getdefaultencoding(), "ignore") for stream in process.communicate())

    if process.returncode != 0:
        raise AirflowConfigException(
            f"Cannot execute {command}. Error code is: {process.returncode}. "
            f"Output: {output}, Stderr: {stderr}"
        )

    return output


def _get_config_value_from_secret_backend(config_key: str) -> str | None:
    """Get Config option values from Secret Backend."""
    try:
        secrets_client = get_custom_secret_backend()
        if not secrets_client:
            return None
        return secrets_client.get_config(config_key)
    except Exception as e:
        raise AirflowConfigException(
            "Cannot retrieve config from alternative secrets backend. "
            "Make sure it is configured properly and that the Backend "
            "is accessible.\n"
            f"{e}"
        )


def _is_template(configuration_description: dict[str, dict[str, Any]], section: str, key: str) -> bool:
    """
    Check if the config is a template.

    :param configuration_description: description of configuration
    :param section: section
    :param key: key
    :return: True if the config is a template
    """
    return configuration_description.get(section, {}).get(key, {}).get("is_template", False)


def _default_config_file_path(file_name: str) -> str:
    templates_dir = os.path.join(os.path.dirname(__file__), "config_templates")
    return os.path.join(templates_dir, file_name)


def retrieve_configuration_description(
    include_airflow: bool = True,
    include_providers: bool = True,
    selected_provider: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Read Airflow configuration description from YAML file.

    :param include_airflow: Include Airflow configs
    :param include_providers: Include provider configs
    :param selected_provider: If specified, include selected provider only
    :return: Python dictionary containing configs & their info
    """
    base_configuration_description: dict[str, dict[str, Any]] = {}
    if include_airflow:
        with open(_default_config_file_path("config.yml")) as config_file:
            base_configuration_description.update(yaml.safe_load(config_file))
    if include_providers:
        from airflow.providers_manager import ProvidersManager

        for provider, config in ProvidersManager().provider_configs:
            if not selected_provider or provider == selected_provider:
                base_configuration_description.update(config)
    return base_configuration_description


class AirflowConfigParser(ConfigParser):
    """
    Custom Airflow Configparser supporting defaults and deprecated options.

    This is a subclass of ConfigParser that supports defaults and deprecated options.

    The defaults are stored in the ``_default_values``. The configuration description keeps
    description of all the options available in Airflow (description follow config.yaml.schema).

    :param default_config: default configuration (in the form of ini file).
    :param configuration_description: description of configuration to use
    """

    def __init__(
        self,
        default_config: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.configuration_description = retrieve_configuration_description(include_providers=False)
        self.upgraded_values = {}
        # For those who would like to use a different data structure to keep defaults:
        # We have to keep the default values in a ConfigParser rather than in any other
        # data structure, because the values we have might contain %% which are ConfigParser
        # interpolation placeholders. The _default_values config parser will interpolate them
        # properly when we call get() on it.
        self._default_values = create_default_config_parser(self.configuration_description)
        self._provider_config_fallback_default_values = create_provider_config_fallback_defaults()
        if default_config is not None:
            self._update_defaults_from_string(default_config)
        self._update_logging_deprecated_template_to_one_from_defaults()
        self.is_validated = False
        self._suppress_future_warnings = False
        self._providers_configuration_loaded = False

    def _update_logging_deprecated_template_to_one_from_defaults(self):
        default = self.get_default_value("logging", "log_filename_template")
        if default:
            # Tuple does not support item assignment, so we have to create a new tuple and replace it
            original_replacement = self.deprecated_values["logging"]["log_filename_template"]
            self.deprecated_values["logging"]["log_filename_template"] = (
                original_replacement[0],
                default,
            )

    def is_template(self, section: str, key) -> bool:
        """
        Return whether the value is templated.

        :param section: section of the config
        :param key: key in the section
        :return: True if the value is templated
        """
        if self.configuration_description is None:
            return False
        return _is_template(self.configuration_description, section, key)

    def _update_defaults_from_string(self, config_string: str):
        """
        Update the defaults in _default_values based on values in config_string ("ini" format).

        Note that those values are not validated and cannot contain variables because we are using
        regular config parser to load them. This method is used to test the config parser in unit tests.

        :param config_string:  ini-formatted config string
        """
        parser = ConfigParser()
        parser.read_string(config_string)
        for section in parser.sections():
            if section not in self._default_values.sections():
                self._default_values.add_section(section)
            errors = False
            for key, value in parser.items(section):
                if not self.is_template(section, key) and "{" in value:
                    errors = True
                    log.error(
                        "The %s.%s value %s read from string contains variable. This is not supported",
                        section,
                        key,
                        value,
                    )
                self._default_values.set(section, key, value)
            if errors:
                raise AirflowConfigException(
                    f"The string config passed as default contains variables. "
                    f"This is not supported. String config: {config_string}"
                )

    def get_default_value(self, section: str, key: str, fallback: Any = None, raw=False, **kwargs) -> Any:
        """
        Retrieve default value from default config parser.

        This will retrieve the default value from the default config parser. Optionally a raw, stored
        value can be retrieved by setting skip_interpolation to True. This is useful for example when
        we want to write the default value to a file, and we don't want the interpolation to happen
        as it is going to be done later when the config is read.

        :param section: section of the config
        :param key: key to use
        :param fallback: fallback value to use
        :param raw: if raw, then interpolation will be reversed
        :param kwargs: other args
        :return:
        """
        value = self._default_values.get(section, key, fallback=fallback, **kwargs)
        if raw and value is not None:
            return value.replace("%", "%%")
        return value

    def get_provider_config_fallback_defaults(self, section: str, key: str, **kwargs) -> Any:
        """Get provider config fallback default values."""
        return self._provider_config_fallback_default_values.get(section, key, fallback=None, **kwargs)

    # These configuration elements can be fetched as the stdout of commands
    # following the "{section}__{name}_cmd" pattern, the idea behind this
    # is to not store password on boxes in text files.
    # These configs can also be fetched from Secrets backend
    # following the "{section}__{name}__secret" pattern
    @functools.cached_property
    def sensitive_config_values(self) -> set[tuple[str, str]]:
        if self.configuration_description is None:
            return set()
        flattened = {
            (s, k): item
            for s, s_c in self.configuration_description.items()
            for k, item in s_c.get("options").items()  # type: ignore[union-attr]
        }
        sensitive = {
            (section.lower(), key.lower())
            for (section, key), v in flattened.items()
            if v.get("sensitive") is True
        }
        depr_option = {self.deprecated_options[x][:-1] for x in sensitive if x in self.deprecated_options}
        depr_section = {
            (self.deprecated_sections[s][0], k) for s, k in sensitive if s in self.deprecated_sections
        }
        sensitive.update(depr_section, depr_option)
        return sensitive

    # A mapping of (new section, new option) -> (old section, old option, since_version).
    # When reading new option, the old option will be checked to see if it exists. If it does a
    # DeprecationWarning will be issued and the old option will be used instead
    deprecated_options: dict[tuple[str, str], tuple[str, str, str]] = {
        ("dag_processor", "refresh_interval"): ("scheduler", "dag_dir_list_interval", "3.0"),
        ("api", "host"): ("webserver", "web_server_host", "3.0"),
        ("api", "port"): ("webserver", "web_server_port", "3.0"),
        ("api", "workers"): ("webserver", "workers", "3.0"),
        ("api", "worker_timeout"): ("webserver", "web_server_worker_timeout", "3.0"),
        ("api", "ssl_cert"): ("webserver", "web_server_ssl_cert", "3.0"),
        ("api", "ssl_key"): ("webserver", "web_server_ssl_key", "3.0"),
        ("api", "access_logfile"): ("webserver", "access_logfile", "3.0"),
        ("triggerer", "capacity"): ("triggerer", "default_capacity", "3.0"),
        ("api", "expose_config"): ("webserver", "expose_config", "3.0.1"),
        ("fab", "access_denied_message"): ("webserver", "access_denied_message", "3.0.2"),
        ("fab", "expose_hostname"): ("webserver", "expose_hostname", "3.0.2"),
        ("fab", "navbar_color"): ("webserver", "navbar_color", "3.0.2"),
        ("fab", "navbar_text_color"): ("webserver", "navbar_text_color", "3.0.2"),
        ("fab", "navbar_hover_color"): ("webserver", "navbar_hover_color", "3.0.2"),
        ("fab", "navbar_text_hover_color"): ("webserver", "navbar_text_hover_color", "3.0.2"),
        ("api", "secret_key"): ("webserver", "secret_key", "3.0.2"),
        ("api", "enable_swagger_ui"): ("webserver", "enable_swagger_ui", "3.0.2"),
        ("dag_processor", "parsing_pre_import_modules"): ("scheduler", "parsing_pre_import_modules", "3.0.4"),
        ("api", "grid_view_sorting_order"): ("webserver", "grid_view_sorting_order", "3.1.0"),
        ("api", "log_fetch_timeout_sec"): ("webserver", "log_fetch_timeout_sec", "3.1.0"),
        ("api", "hide_paused_dags_by_default"): ("webserver", "hide_paused_dags_by_default", "3.1.0"),
        ("api", "page_size"): ("webserver", "page_size", "3.1.0"),
        ("api", "default_wrap"): ("webserver", "default_wrap", "3.1.0"),
        ("api", "auto_refresh_interval"): ("webserver", "auto_refresh_interval", "3.1.0"),
        ("api", "require_confirmation_dag_change"): ("webserver", "require_confirmation_dag_change", "3.1.0"),
        ("api", "instance_name"): ("webserver", "instance_name", "3.1.0"),
        ("api", "log_config"): ("api", "access_logfile", "3.1.0"),
    }

    # A mapping of new section -> (old section, since_version).
    deprecated_sections: dict[str, tuple[str, str]] = {}

    # Now build the inverse so we can go from old_section/old_key to new_section/new_key
    # if someone tries to retrieve it based on old_section/old_key
    @functools.cached_property
    def inversed_deprecated_options(self):
        return {(sec, name): key for key, (sec, name, ver) in self.deprecated_options.items()}

    @functools.cached_property
    def inversed_deprecated_sections(self):
        return {
            old_section: new_section for new_section, (old_section, ver) in self.deprecated_sections.items()
        }

    # A mapping of old default values that we want to change and warn the user
    # about. Mapping of section -> setting -> { old, replace }
    deprecated_values: dict[str, dict[str, tuple[Pattern, str]]] = {
        "logging": {
            "log_filename_template": (
                re.compile(
                    re.escape(
                        "dag_id={{ ti.dag_id }}/run_id={{ ti.run_id }}/task_id={{ ti.task_id }}/{% if ti.map_index >= 0 %}map_index={{ ti.map_index }}/{% endif %}attempt={{ try_number }}.log"
                    )
                ),
                # The actual replacement value will be updated after defaults are loaded from config.yml
                "XX-set-after-default-config-loaded-XX",
            ),
        },
        "core": {
            "executor": (re.compile(re.escape("SequentialExecutor")), "LocalExecutor"),
        },
    }

    _available_logging_levels = ["CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG"]
    enums_options = {
        ("core", "default_task_weight_rule"): sorted(WeightRule.all_weight_rules()),
        ("core", "dag_ignore_file_syntax"): ["regexp", "glob"],
        ("core", "mp_start_method"): multiprocessing.get_all_start_methods(),
        ("dag_processor", "file_parsing_sort_mode"): [
            "modified_time",
            "random_seeded_by_host",
            "alphabetical",
        ],
        ("logging", "logging_level"): _available_logging_levels,
        ("logging", "fab_logging_level"): _available_logging_levels,
        # celery_logging_level can be empty, which uses logging_level as fallback
        ("logging", "celery_logging_level"): [*_available_logging_levels, ""],
        ("webserver", "analytical_tool"): ["google_analytics", "metarouter", "segment", "matomo", ""],
        ("api", "grid_view_sorting_order"): ["topological", "hierarchical_alphabetical"],
    }

    upgraded_values: dict[tuple[str, str], str]
    """Mapping of (section,option) to the old value that was upgraded"""

    def get_sections_including_defaults(self) -> list[str]:
        """
        Retrieve all sections from the configuration parser, including sections defined by built-in defaults.

        :return: list of section names
        """
        return list(dict.fromkeys(itertools.chain(self.configuration_description, self.sections())))

    def get_options_including_defaults(self, section: str) -> list[str]:
        """
        Retrieve all possible option from the configuration parser for the section given.

        Includes options defined by built-in defaults.

        :return: list of option names for the section given
        """
        my_own_options = self.options(section) if self.has_section(section) else []
        all_options_from_defaults = self.configuration_description.get(section, {}).get("options", {})
        return list(dict.fromkeys(itertools.chain(all_options_from_defaults, my_own_options)))

    def optionxform(self, optionstr: str) -> str:
        """
        Transform option names on every read, get, or set operation.

        This changes from the default behaviour of ConfigParser from lower-casing
        to instead be case-preserving.

        :param optionstr:
        :return:
        """
        return optionstr

    @contextmanager
    def make_sure_configuration_loaded(self, with_providers: bool) -> Generator[None, None, None]:
        """
        Make sure configuration is loaded with or without providers.

        This happens regardless if the provider configuration has been loaded before or not.
        Restores configuration to the state before entering the context.

        :param with_providers: whether providers should be loaded
        """
        reload_providers_when_leaving = False
        if with_providers and not self._providers_configuration_loaded:
            # make sure providers are initialized
            from airflow.providers_manager import ProvidersManager

            # run internal method to initialize providers configuration in ordered to not trigger the
            # initialize_providers_configuration cache (because we will be unloading it now
            ProvidersManager()._initialize_providers_configuration()
        elif not with_providers and self._providers_configuration_loaded:
            reload_providers_when_leaving = True
            self.restore_core_default_configuration()
        yield
        if reload_providers_when_leaving:
            self.load_providers_configuration()

    @staticmethod
    def _write_section_header(
        file: IO[str],
        include_descriptions: bool,
        section_config_description: dict[str, str],
        section_to_write: str,
    ) -> None:
        """Write header for configuration section."""
        file.write(f"[{section_to_write}]\n")
        section_description = section_config_description.get("description")
        if section_description and include_descriptions:
            for line in section_description.splitlines():
                file.write(f"# {line}\n")
            file.write("\n")

    def _write_option_header(
        self,
        file: IO[str],
        option: str,
        extra_spacing: bool,
        include_descriptions: bool,
        include_env_vars: bool,
        include_examples: bool,
        include_sources: bool,
        section_config_description: dict[str, dict[str, Any]],
        section_to_write: str,
        sources_dict: ConfigSourcesType,
    ) -> tuple[bool, bool]:
        """
        Write header for configuration option.

        Returns tuple of (should_continue, needs_separation) where needs_separation should be
        set if the option needs additional separation to visually separate it from the next option.
        """
        from airflow import __version__ as airflow_version

        option_config_description = (
            section_config_description.get("options", {}).get(option, {})
            if section_config_description
            else {}
        )
        version_added = option_config_description.get("version_added")
        if version_added is not None and parse_version(version_added) > parse_version(
            parse_version(airflow_version).base_version
        ):
            # skip if option is going to be added in the future version
            return False, False
        description = option_config_description.get("description")
        needs_separation = False
        if description and include_descriptions:
            for line in description.splitlines():
                file.write(f"# {line}\n")
            needs_separation = True
        example = option_config_description.get("example")
        if example is not None and include_examples:
            if extra_spacing:
                file.write("#\n")
            example_lines = example.splitlines()
            example = "\n# ".join(example_lines)
            file.write(f"# Example: {option} = {example}\n")
            needs_separation = True
        if include_sources and sources_dict:
            sources_section = sources_dict.get(section_to_write)
            value_with_source = sources_section.get(option) if sources_section else None
            if value_with_source is None:
                file.write("#\n# Source: not defined\n")
            else:
                file.write(f"#\n# Source: {value_with_source[1]}\n")
            needs_separation = True
        if include_env_vars:
            file.write(f"#\n# Variable: AIRFLOW__{section_to_write.upper()}__{option.upper()}\n")
            if extra_spacing:
                file.write("#\n")
            needs_separation = True
        return True, needs_separation

    def _write_value(
        self,
        file: IO[str],
        option: str,
        comment_out_everything: bool,
        needs_separation: bool,
        only_defaults: bool,
        section_to_write: str,
    ):
        if self._default_values is None:
            default_value = None
        else:
            default_value = self.get_default_value(section_to_write, option, raw=True)
        if only_defaults:
            value = default_value
        else:
            value = self.get(section_to_write, option, fallback=default_value, raw=True)
        if value is None:
            file.write(f"# {option} = \n")
        else:
            if comment_out_everything:
                value_lines = value.splitlines()
                value = "\n# ".join(value_lines)
                file.write(f"# {option} = {value}\n")
            else:
                file.write(f"{option} = {value}\n")
        if needs_separation:
            file.write("\n")

    def write_custom_config(
        self,
        file: IO[str],
        comment_out_defaults: bool = True,
        include_descriptions: bool = True,
        extra_spacing: bool = True,
        modifications: ConfigModifications | None = None,
    ) -> None:
        """
        Write a configuration file using a ConfigModifications object.

        This method includes only options from the current airflow.cfg. For each option:
          - If it's marked for removal, omit it.
          - If renamed, output it under its new name and add a comment indicating its original location.
          - If a default update is specified, apply the new default and output the option as a commented line.
          - Otherwise, if the current value equals the default and comment_out_defaults is True, output it as a comment.
        Options absent from the current airflow.cfg are omitted.

        :param file: File to write the configuration.
        :param comment_out_defaults: If True, options whose value equals the default are written as comments.
        :param include_descriptions: Whether to include section descriptions.
        :param extra_spacing: Whether to insert an extra blank line after each option.
        :param modifications: ConfigModifications instance with rename, remove, and default updates.
        """
        modifications = modifications or ConfigModifications()
        output: dict[str, list[tuple[str, str, bool, str]]] = {}

        for section in self._sections:  # type: ignore[attr-defined]  # accessing _sections from ConfigParser
            for option, orig_value in self._sections[section].items():  # type: ignore[attr-defined]
                key = (section.lower(), option.lower())
                if key in modifications.remove:
                    continue

                mod_comment = ""
                if key in modifications.rename:
                    new_sec, new_opt = modifications.rename[key]
                    effective_section = new_sec
                    effective_option = new_opt
                    mod_comment += f"# Renamed from {section}.{option}\n"
                else:
                    effective_section = section
                    effective_option = option

                value = orig_value
                if key in modifications.default_updates:
                    mod_comment += (
                        f"# Default updated from {orig_value} to {modifications.default_updates[key]}\n"
                    )
                    value = modifications.default_updates[key]

                default_value = self.get_default_value(effective_section, effective_option, fallback="")
                is_default = str(value) == str(default_value)
                output.setdefault(effective_section.lower(), []).append(
                    (effective_option, str(value), is_default, mod_comment)
                )

        for section, options in output.items():
            section_buffer = StringIO()
            section_buffer.write(f"[{section}]\n")
            if include_descriptions:
                description = self.configuration_description.get(section, {}).get("description", "")
                if description:
                    for line in description.splitlines():
                        section_buffer.write(f"# {line}\n")
                    section_buffer.write("\n")
            for option, value_str, is_default, mod_comment in options:
                key = (section.lower(), option.lower())
                if key in modifications.default_updates and comment_out_defaults:
                    section_buffer.write(f"# {option} = {value_str}\n")
                else:
                    if mod_comment:
                        section_buffer.write(mod_comment)
                    if is_default and comment_out_defaults:
                        section_buffer.write(f"# {option} = {value_str}\n")
                    else:
                        section_buffer.write(f"{option} = {value_str}\n")
                if extra_spacing:
                    section_buffer.write("\n")
            content = section_buffer.getvalue().strip()
            if content:
                file.write(f"{content}\n\n")

    def write(  # type: ignore[override]
        self,
        file: IO[str],
        section: str | None = None,
        include_examples: bool = True,
        include_descriptions: bool = True,
        include_sources: bool = True,
        include_env_vars: bool = True,
        include_providers: bool = True,
        comment_out_everything: bool = False,
        hide_sensitive_values: bool = False,
        extra_spacing: bool = True,
        only_defaults: bool = False,
        **kwargs: Any,
    ) -> None:
        """
        Write configuration with comments and examples to a file.

        :param file: file to write to
        :param section: section of the config to write, defaults to all sections
        :param include_examples: Include examples in the output
        :param include_descriptions: Include descriptions in the output
        :param include_sources: Include the source of each config option
        :param include_env_vars: Include environment variables corresponding to each config option
        :param include_providers: Include providers configuration
        :param comment_out_everything: Comment out all values
        :param hide_sensitive_values: Include sensitive values in the output
        :param extra_spacing: Add extra spacing before examples and after variables
        :param only_defaults: Only include default values when writing the config, not the actual values
        """
        sources_dict = {}
        if include_sources:
            sources_dict = self.as_dict(display_source=True)
        if self._default_values is None:
            raise RuntimeError("Cannot write default config, no default config set")
        if self.configuration_description is None:
            raise RuntimeError("Cannot write default config, no default configuration description set")
        with self.make_sure_configuration_loaded(with_providers=include_providers):
            for section_to_write in self.get_sections_including_defaults():
                section_config_description = self.configuration_description.get(section_to_write, {})
                if section_to_write != section and section is not None:
                    continue
                if self._default_values.has_section(section_to_write) or self.has_section(section_to_write):
                    self._write_section_header(
                        file, include_descriptions, section_config_description, section_to_write
                    )
                    for option in self.get_options_including_defaults(section_to_write):
                        should_continue, needs_separation = self._write_option_header(
                            file=file,
                            option=option,
                            extra_spacing=extra_spacing,
                            include_descriptions=include_descriptions,
                            include_env_vars=include_env_vars,
                            include_examples=include_examples,
                            include_sources=include_sources,
                            section_config_description=section_config_description,
                            section_to_write=section_to_write,
                            sources_dict=sources_dict,
                        )
                        self._write_value(
                            file=file,
                            option=option,
                            comment_out_everything=comment_out_everything,
                            needs_separation=needs_separation,
                            only_defaults=only_defaults,
                            section_to_write=section_to_write,
                        )
                    if include_descriptions and not needs_separation:
                        # extra separation between sections in case last option did not need it
                        file.write("\n")

    def restore_core_default_configuration(self) -> None:
        """
        Restore default configuration for core Airflow.

        It does not restore configuration for providers. If you want to restore configuration for
        providers, you need to call ``load_providers_configuration`` method.
        """
        self.configuration_description = retrieve_configuration_description(include_providers=False)
        self._default_values = create_default_config_parser(self.configuration_description)
        self._providers_configuration_loaded = False

    def validate(self):
        self._validate_sqlite3_version()
        self._validate_enums()

        for section, replacement in self.deprecated_values.items():
            for name, info in replacement.items():
                old, new = info
                current_value = self.get(section, name, fallback="")
                if self._using_old_value(old, current_value):
                    self.upgraded_values[(section, name)] = current_value
                    new_value = old.sub(new, current_value)
                    self._update_env_var(section=section, name=name, new_value=new_value)
                    self._create_future_warning(
                        name=name,
                        section=section,
                        current_value=current_value,
                        new_value=new_value,
                    )

        self._upgrade_postgres_metastore_conn()
        self.is_validated = True

    def _upgrade_postgres_metastore_conn(self):
        """
        Upgrade SQL schemas.

        As of SQLAlchemy 1.4, schemes `postgres+psycopg2` and `postgres`
        must be replaced with `postgresql`.
        """
        section, key = "database", "sql_alchemy_conn"
        old_value = self.get(section, key, _extra_stacklevel=1)
        bad_schemes = ["postgres+psycopg2", "postgres"]
        good_scheme = "postgresql"
        parsed = urlsplit(old_value)
        if parsed.scheme in bad_schemes:
            warnings.warn(
                f"Bad scheme in Airflow configuration core > sql_alchemy_conn: `{parsed.scheme}`. "
                "As of SQLAlchemy 1.4 (adopted in Airflow 2.3) this is no longer supported.  You must "
                f"change to `{good_scheme}` before the next Airflow release.",
                FutureWarning,
                stacklevel=1,
            )
            self.upgraded_values[(section, key)] = old_value
            new_value = re.sub("^" + re.escape(f"{parsed.scheme}://"), f"{good_scheme}://", old_value)
            self._update_env_var(section=section, name=key, new_value=new_value)

            # if the old value is set via env var, we need to wipe it
            # otherwise, it'll "win" over our adjusted value
            old_env_var = self._env_var_name("core", key)
            os.environ.pop(old_env_var, None)

    def _validate_enums(self):
        """Validate that enum type config has an accepted value."""
        for (section_key, option_key), enum_options in self.enums_options.items():
            if self.has_option(section_key, option_key):
                value = self.get(section_key, option_key, fallback=None)
                if value and value not in enum_options:
                    raise AirflowConfigException(
                        f"`[{section_key}] {option_key}` should not be "
                        f"{value!r}. Possible values: {', '.join(enum_options)}."
                    )

    def _validate_sqlite3_version(self):
        """
        Validate SQLite version.

        Some features in storing rendered fields require SQLite >= 3.15.0.
        """
        if "sqlite" not in self.get("database", "sql_alchemy_conn"):
            return

        import sqlite3

        min_sqlite_version = (3, 15, 0)
        if _parse_sqlite_version(sqlite3.sqlite_version) >= min_sqlite_version:
            return

        from airflow.utils.docs import get_docs_url

        min_sqlite_version_str = ".".join(str(s) for s in min_sqlite_version)
        raise AirflowConfigException(
            f"error: SQLite C library too old (< {min_sqlite_version_str}). "
            f"See {get_docs_url('howto/set-up-database.html#setting-up-a-sqlite-database')}"
        )

    def _using_old_value(self, old: Pattern, current_value: str) -> bool:
        return old.search(current_value) is not None

    def _update_env_var(self, section: str, name: str, new_value: str):
        env_var = self._env_var_name(section, name)
        # Set it as an env var so that any subprocesses keep the same override!
        os.environ[env_var] = new_value

    @staticmethod
    def _create_future_warning(name: str, section: str, current_value: Any, new_value: Any):
        warnings.warn(
            f"The {name!r} setting in [{section}] has the old default value of {current_value!r}. "
            f"This value has been changed to {new_value!r} in the running config, but please update your config.",
            FutureWarning,
            stacklevel=3,
        )

    def mask_secrets(self):
        from airflow.sdk.execution_time.secrets_masker import mask_secret

        for section, key in self.sensitive_config_values:
            try:
                with self.suppress_future_warnings():
                    value = self.get(section, key, suppress_warnings=True)
            except AirflowConfigException:
                log.debug(
                    "Could not retrieve value from section %s, for key %s. Skipping redaction of this conf.",
                    section,
                    key,
                )
                continue
            mask_secret(value)

    def _env_var_name(self, section: str, key: str) -> str:
        return f"{ENV_VAR_PREFIX}{section.replace('.', '_').upper()}__{key.upper()}"

    def _get_env_var_option(self, section: str, key: str):
        # must have format AIRFLOW__{SECTION}__{KEY} (note double underscore)
        env_var = self._env_var_name(section, key)
        if env_var in os.environ:
            return expand_env_var(os.environ[env_var])
        # alternatively AIRFLOW__{SECTION}__{KEY}_CMD (for a command)
        env_var_cmd = env_var + "_CMD"
        if env_var_cmd in os.environ:
            # if this is a valid command key...
            if (section, key) in self.sensitive_config_values:
                return run_command(os.environ[env_var_cmd])
        # alternatively AIRFLOW__{SECTION}__{KEY}_SECRET (to get from Secrets Backend)
        env_var_secret_path = env_var + "_SECRET"
        if env_var_secret_path in os.environ:
            # if this is a valid secret path...
            if (section, key) in self.sensitive_config_values:
                return _get_config_value_from_secret_backend(os.environ[env_var_secret_path])
        return None

    def _get_cmd_option(self, section: str, key: str):
        fallback_key = key + "_cmd"
        if (section, key) in self.sensitive_config_values:
            if super().has_option(section, fallback_key):
                command = super().get(section, fallback_key)
                return run_command(command)
        return None

    def _get_cmd_option_from_config_sources(
        self, config_sources: ConfigSourcesType, section: str, key: str
    ) -> str | None:
        fallback_key = key + "_cmd"
        if (section, key) in self.sensitive_config_values:
            section_dict = config_sources.get(section)
            if section_dict is not None:
                command_value = section_dict.get(fallback_key)
                if command_value is not None:
                    if isinstance(command_value, str):
                        command = command_value
                    else:
                        command = command_value[0]
                    return run_command(command)
        return None

    def _get_secret_option(self, section: str, key: str) -> str | None:
        """Get Config option values from Secret Backend."""
        fallback_key = key + "_secret"
        if (section, key) in self.sensitive_config_values:
            if super().has_option(section, fallback_key):
                secrets_path = super().get(section, fallback_key)
                return _get_config_value_from_secret_backend(secrets_path)
        return None

    def _get_secret_option_from_config_sources(
        self, config_sources: ConfigSourcesType, section: str, key: str
    ) -> str | None:
        fallback_key = key + "_secret"
        if (section, key) in self.sensitive_config_values:
            section_dict = config_sources.get(section)
            if section_dict is not None:
                secrets_path_value = section_dict.get(fallback_key)
                if secrets_path_value is not None:
                    if isinstance(secrets_path_value, str):
                        secrets_path = secrets_path_value
                    else:
                        secrets_path = secrets_path_value[0]
                    return _get_config_value_from_secret_backend(secrets_path)
        return None

    def get_mandatory_value(self, section: str, key: str, **kwargs) -> str:
        value = self.get(section, key, _extra_stacklevel=1, **kwargs)
        if value is None:
            raise ValueError(f"The value {section}/{key} should be set!")
        return value

    def get_mandatory_list_value(self, section: str, key: str, **kwargs) -> list[str]:
        value = self.getlist(section, key, **kwargs)
        if value is None:
            raise ValueError(f"The value {section}/{key} should be set!")
        return value

    @overload  # type: ignore[override]
    def get(self, section: str, key: str, fallback: str = ..., **kwargs) -> str: ...

    @overload
    def get(self, section: str, key: str, **kwargs) -> str | None: ...

    def get(  # type: ignore[misc]
        self,
        section: str,
        key: str,
        suppress_warnings: bool = False,
        lookup_from_deprecated: bool = True,
        _extra_stacklevel: int = 0,
        **kwargs,
    ) -> str | None:
        section = section.lower()
        key = key.lower()
        warning_emitted = False
        deprecated_section: str | None = None
        deprecated_key: str | None = None

        if lookup_from_deprecated:
            option_description = (
                self.configuration_description.get(section, {}).get("options", {}).get(key, {})
            )
            if option_description.get("deprecated"):
                deprecation_reason = option_description.get("deprecation_reason", "")
                warnings.warn(
                    f"The '{key}' option in section {section} is deprecated. {deprecation_reason}",
                    DeprecationWarning,
                    stacklevel=2 + _extra_stacklevel,
                )
            # For the cases in which we rename whole sections
            if section in self.inversed_deprecated_sections:
                deprecated_section, deprecated_key = (section, key)
                section = self.inversed_deprecated_sections[section]
                if not self._suppress_future_warnings:
                    warnings.warn(
                        f"The config section [{deprecated_section}] has been renamed to "
                        f"[{section}]. Please update your `conf.get*` call to use the new name",
                        FutureWarning,
                        stacklevel=2 + _extra_stacklevel,
                    )
                # Don't warn about individual rename if the whole section is renamed
                warning_emitted = True
            elif (section, key) in self.inversed_deprecated_options:
                # Handle using deprecated section/key instead of the new section/key
                new_section, new_key = self.inversed_deprecated_options[(section, key)]
                if not self._suppress_future_warnings and not warning_emitted:
                    warnings.warn(
                        f"section/key [{section}/{key}] has been deprecated, you should use"
                        f"[{new_section}/{new_key}] instead. Please update your `conf.get*` call to use the "
                        "new name",
                        FutureWarning,
                        stacklevel=2 + _extra_stacklevel,
                    )
                    warning_emitted = True
                deprecated_section, deprecated_key = section, key
                section, key = (new_section, new_key)
            elif section in self.deprecated_sections:
                # When accessing the new section name, make sure we check under the old config name
                deprecated_key = key
                deprecated_section = self.deprecated_sections[section][0]
            else:
                deprecated_section, deprecated_key, _ = self.deprecated_options.get(
                    (section, key), (None, None, None)
                )
        # first check environment variables
        option = self._get_environment_variables(
            deprecated_key,
            deprecated_section,
            key,
            section,
            issue_warning=not warning_emitted,
            extra_stacklevel=_extra_stacklevel,
        )
        if option is not None:
            return option

        # ...then the config file
        option = self._get_option_from_config_file(
            deprecated_key,
            deprecated_section,
            key,
            kwargs,
            section,
            issue_warning=not warning_emitted,
            extra_stacklevel=_extra_stacklevel,
        )
        if option is not None:
            return option

        # ...then commands
        option = self._get_option_from_commands(
            deprecated_key,
            deprecated_section,
            key,
            section,
            issue_warning=not warning_emitted,
            extra_stacklevel=_extra_stacklevel,
        )
        if option is not None:
            return option

        # ...then from secret backends
        option = self._get_option_from_secrets(
            deprecated_key,
            deprecated_section,
            key,
            section,
            issue_warning=not warning_emitted,
            extra_stacklevel=_extra_stacklevel,
        )
        if option is not None:
            return option

        # ...then the default config
        if self.get_default_value(section, key) is not None or "fallback" in kwargs:
            return expand_env_var(self.get_default_value(section, key, **kwargs))

        if self.get_provider_config_fallback_defaults(section, key) is not None:
            # no expansion needed
            return self.get_provider_config_fallback_defaults(section, key, **kwargs)

        if not suppress_warnings:
            log.warning("section/key [%s/%s] not found in config", section, key)

        raise AirflowConfigException(f"section/key [{section}/{key}] not found in config")

    def _get_option_from_secrets(
        self,
        deprecated_key: str | None,
        deprecated_section: str | None,
        key: str,
        section: str,
        issue_warning: bool = True,
        extra_stacklevel: int = 0,
    ) -> str | None:
        option = self._get_secret_option(section, key)
        if option:
            return option
        if deprecated_section and deprecated_key:
            with self.suppress_future_warnings():
                option = self._get_secret_option(deprecated_section, deprecated_key)
            if option:
                if issue_warning:
                    self._warn_deprecate(section, key, deprecated_section, deprecated_key, extra_stacklevel)
                return option
        return None

    def _get_option_from_commands(
        self,
        deprecated_key: str | None,
        deprecated_section: str | None,
        key: str,
        section: str,
        issue_warning: bool = True,
        extra_stacklevel: int = 0,
    ) -> str | None:
        option = self._get_cmd_option(section, key)
        if option:
            return option
        if deprecated_section and deprecated_key:
            with self.suppress_future_warnings():
                option = self._get_cmd_option(deprecated_section, deprecated_key)
            if option:
                if issue_warning:
                    self._warn_deprecate(section, key, deprecated_section, deprecated_key, extra_stacklevel)
                return option
        return None

    def _get_option_from_config_file(
        self,
        deprecated_key: str | None,
        deprecated_section: str | None,
        key: str,
        kwargs: dict[str, Any],
        section: str,
        issue_warning: bool = True,
        extra_stacklevel: int = 0,
    ) -> str | None:
        if super().has_option(section, key):
            # Use the parent's methods to get the actual config here to be able to
            # separate the config from default config.
            return expand_env_var(super().get(section, key, **kwargs))
        if deprecated_section and deprecated_key:
            if super().has_option(deprecated_section, deprecated_key):
                if issue_warning:
                    self._warn_deprecate(section, key, deprecated_section, deprecated_key, extra_stacklevel)
                with self.suppress_future_warnings():
                    return expand_env_var(super().get(deprecated_section, deprecated_key, **kwargs))
        return None

    def _get_environment_variables(
        self,
        deprecated_key: str | None,
        deprecated_section: str | None,
        key: str,
        section: str,
        issue_warning: bool = True,
        extra_stacklevel: int = 0,
    ) -> str | None:
        option = self._get_env_var_option(section, key)
        if option is not None:
            return option
        if deprecated_section and deprecated_key:
            with self.suppress_future_warnings():
                option = self._get_env_var_option(deprecated_section, deprecated_key)
            if option is not None:
                if issue_warning:
                    self._warn_deprecate(section, key, deprecated_section, deprecated_key, extra_stacklevel)
                return option
        return None

    def getboolean(self, section: str, key: str, **kwargs) -> bool:  # type: ignore[override]
        val = str(self.get(section, key, _extra_stacklevel=1, **kwargs)).lower().strip()
        if "#" in val:
            val = val.split("#")[0].strip()
        if val in ("t", "true", "1"):
            return True
        if val in ("f", "false", "0"):
            return False
        raise AirflowConfigException(
            f'Failed to convert value to bool. Please check "{key}" key in "{section}" section. '
            f'Current value: "{val}".'
        )

    def getint(self, section: str, key: str, **kwargs) -> int:  # type: ignore[override]
        val = self.get(section, key, _extra_stacklevel=1, **kwargs)
        if val is None:
            raise AirflowConfigException(
                f"Failed to convert value None to int. "
                f'Please check "{key}" key in "{section}" section is set.'
            )
        try:
            return int(val)
        except ValueError:
            raise AirflowConfigException(
                f'Failed to convert value to int. Please check "{key}" key in "{section}" section. '
                f'Current value: "{val}".'
            )

    def getfloat(self, section: str, key: str, **kwargs) -> float:  # type: ignore[override]
        val = self.get(section, key, _extra_stacklevel=1, **kwargs)
        if val is None:
            raise AirflowConfigException(
                f"Failed to convert value None to float. "
                f'Please check "{key}" key in "{section}" section is set.'
            )
        try:
            return float(val)
        except ValueError:
            raise AirflowConfigException(
                f'Failed to convert value to float. Please check "{key}" key in "{section}" section. '
                f'Current value: "{val}".'
            )

    def getlist(self, section: str, key: str, delimiter=",", **kwargs):
        val = self.get(section, key, **kwargs)
        if val is None:
            if "fallback" in kwargs:
                return val
            raise AirflowConfigException(
                f"Failed to convert value None to list. "
                f'Please check "{key}" key in "{section}" section is set.'
            )
        try:
            return [item.strip() for item in val.split(delimiter)]
        except Exception:
            raise AirflowConfigException(
                f'Failed to parse value to a list. Please check "{key}" key in "{section}" section. '
                f'Current value: "{val}".'
            )

    def getimport(self, section: str, key: str, **kwargs) -> Any:
        """
        Read options, import the full qualified name, and return the object.

        In case of failure, it throws an exception with the key and section names

        :return: The object or None, if the option is empty
        """
        full_qualified_path = conf.get(section=section, key=key, **kwargs)
        if not full_qualified_path:
            return None

        try:
            return import_string(full_qualified_path)
        except ImportError as e:
            log.warning(e)
            raise AirflowConfigException(
                f'The object could not be loaded. Please check "{key}" key in "{section}" section. '
                f'Current value: "{full_qualified_path}".'
            )

    def getjson(
        self, section: str, key: str, fallback=None, **kwargs
    ) -> dict | list | str | int | float | None:
        """
        Return a config value parsed from a JSON string.

        ``fallback`` is *not* JSON parsed but used verbatim when no config value is given.
        """
        try:
            data = self.get(section=section, key=key, fallback=None, _extra_stacklevel=1, **kwargs)
        except (NoSectionError, NoOptionError):
            data = None

        if data is None or data == "":
            return fallback

        try:
            return json.loads(data)
        except JSONDecodeError as e:
            raise AirflowConfigException(f"Unable to parse [{section}] {key!r} as valid json") from e

    def gettimedelta(
        self, section: str, key: str, fallback: Any = None, **kwargs
    ) -> datetime.timedelta | None:
        """
        Get the config value for the given section and key, and convert it into datetime.timedelta object.

        If the key is missing, then it is considered as `None`.

        :param section: the section from the config
        :param key: the key defined in the given section
        :param fallback: fallback value when no config value is given, defaults to None
        :raises AirflowConfigException: raised because ValueError or OverflowError
        :return: datetime.timedelta(seconds=<config_value>) or None
        """
        val = self.get(section, key, fallback=fallback, _extra_stacklevel=1, **kwargs)

        if val:
            # the given value must be convertible to integer
            try:
                int_val = int(val)
            except ValueError:
                raise AirflowConfigException(
                    f'Failed to convert value to int. Please check "{key}" key in "{section}" section. '
                    f'Current value: "{val}".'
                )

            try:
                return datetime.timedelta(seconds=int_val)
            except OverflowError as err:
                raise AirflowConfigException(
                    f"Failed to convert value to timedelta in `seconds`. "
                    f"{err}. "
                    f'Please check "{key}" key in "{section}" section. Current value: "{val}".'
                )

        return fallback

    def read(
        self,
        filenames: str | bytes | os.PathLike | Iterable[str | bytes | os.PathLike],
        encoding=None,
    ):
        super().read(filenames=filenames, encoding=encoding)

    def read_dict(  # type: ignore[override]
        self, dictionary: dict[str, dict[str, Any]], source: str = "<dict>"
    ):
        """
        We define a different signature here to add better type hints and checking.

        :param dictionary: dictionary to read from
        :param source: source to be used to store the configuration
        :return:
        """
        super().read_dict(dictionary=dictionary, source=source)

    def has_option(self, section: str, option: str, lookup_from_deprecated: bool = True) -> bool:
        """
        Check if option is defined.

        Uses self.get() to avoid reimplementing the priority order of config variables
        (env, config, cmd, defaults).

        :param section: section to get option from
        :param option: option to get
        :param lookup_from_deprecated: If True, check if the option is defined in deprecated sections
        :return:
        """
        try:
            value = self.get(
                section,
                option,
                fallback=None,
                _extra_stacklevel=1,
                suppress_warnings=True,
                lookup_from_deprecated=lookup_from_deprecated,
            )
            if value is None:
                return False
            return True
        except (NoOptionError, NoSectionError):
            return False

    def set(self, section: str, option: str, value: str | None = None) -> None:
        """
        Set an option to the given value.

        This override just makes sure the section and option are lower case, to match what we do in `get`.
        """
        section = section.lower()
        option = option.lower()
        defaults = self.configuration_description or {}
        if not self.has_section(section) and section in defaults:
            # Trying to set a key in a section that exists in default, but not in the user config;
            # automatically create it
            self.add_section(section)
        super().set(section, option, value)

    def remove_option(self, section: str, option: str, remove_default: bool = True):
        """
        Remove an option if it exists in config from a file or default config.

        If both of config have the same option, this removes the option
        in both configs unless remove_default=False.
        """
        section = section.lower()
        option = option.lower()
        if super().has_option(section, option):
            super().remove_option(section, option)

        if self.get_default_value(section, option) is not None and remove_default:
            self._default_values.remove_option(section, option)

    def getsection(self, section: str) -> ConfigOptionsDictType | None:
        """
        Return the section as a dict.

        Values are converted to int, float, bool as required.

        :param section: section from the config
        """
        if not self.has_section(section) and not self._default_values.has_section(section):
            return None
        if self._default_values.has_section(section):
            _section: ConfigOptionsDictType = dict(self._default_values.items(section))
        else:
            _section = {}

        if self.has_section(section):
            _section.update(self.items(section))

        section_prefix = self._env_var_name(section, "")
        for env_var in sorted(os.environ.keys()):
            if env_var.startswith(section_prefix):
                key = env_var.replace(section_prefix, "")
                if key.endswith("_CMD"):
                    key = key[:-4]
                key = key.lower()
                _section[key] = self._get_env_var_option(section, key)

        for key, val in _section.items():
            if val is None:
                raise AirflowConfigException(
                    f"Failed to convert value automatically. "
                    f'Please check "{key}" key in "{section}" section is set.'
                )
            try:
                _section[key] = int(val)
            except ValueError:
                try:
                    _section[key] = float(val)
                except ValueError:
                    if isinstance(val, str) and val.lower() in ("t", "true"):
                        _section[key] = True
                    elif isinstance(val, str) and val.lower() in ("f", "false"):
                        _section[key] = False
        return _section

    def as_dict(
        self,
        display_source: bool = False,
        display_sensitive: bool = False,
        raw: bool = False,
        include_env: bool = True,
        include_cmds: bool = True,
        include_secret: bool = True,
    ) -> ConfigSourcesType:
        """
        Return the current configuration as an OrderedDict of OrderedDicts.

        When materializing current configuration Airflow defaults are
        materialized along with user set configs. If any of the `include_*`
        options are False then the result of calling command or secret key
        configs do not override Airflow defaults and instead are passed through.
        In order to then avoid Airflow defaults from overwriting user set
        command or secret key configs we filter out bare sensitive_config_values
        that are set to Airflow defaults when command or secret key configs
        produce different values.

        :param display_source: If False, the option value is returned. If True,
            a tuple of (option_value, source) is returned. Source is either
            'airflow.cfg', 'default', 'env var', or 'cmd'.
        :param display_sensitive: If True, the values of options set by env
            vars and bash commands will be displayed. If False, those options
            are shown as '< hidden >'
        :param raw: Should the values be output as interpolated values, or the
            "raw" form that can be fed back in to ConfigParser
        :param include_env: Should the value of configuration from AIRFLOW__
            environment variables be included or not
        :param include_cmds: Should the result of calling any ``*_cmd`` config be
            set (True, default), or should the _cmd options be left as the
            command to run (False)
        :param include_secret: Should the result of calling any ``*_secret`` config be
            set (True, default), or should the _secret options be left as the
            path to get the secret from (False)
        :return: Dictionary, where the key is the name of the section and the content is
            the dictionary with the name of the parameter and its value.
        """
        if not display_sensitive:
            # We want to hide the sensitive values at the appropriate methods
            # since envs from cmds, secrets can be read at _include_envs method
            if not all([include_env, include_cmds, include_secret]):
                raise ValueError(
                    "If display_sensitive is false, then include_env, "
                    "include_cmds, include_secret must all be set as True"
                )

        config_sources: ConfigSourcesType = {}

        # We check sequentially all those sources and the last one we saw it in will "win"
        configs: Iterable[tuple[str, ConfigParser]] = [
            ("provider-fallback-defaults", self._provider_config_fallback_default_values),
            ("default", self._default_values),
            ("airflow.cfg", self),
        ]

        self._replace_config_with_display_sources(
            config_sources,
            configs,
            self.configuration_description if self.configuration_description else {},
            display_source,
            raw,
            self.deprecated_options,
            include_cmds=include_cmds,
            include_env=include_env,
            include_secret=include_secret,
        )

        # add env vars and overwrite because they have priority
        if include_env:
            self._include_envs(config_sources, display_sensitive, display_source, raw)
        else:
            self._filter_by_source(config_sources, display_source, self._get_env_var_option)

        # add bash commands
        if include_cmds:
            self._include_commands(config_sources, display_sensitive, display_source, raw)
        else:
            self._filter_by_source(config_sources, display_source, self._get_cmd_option)

        # add config from secret backends
        if include_secret:
            self._include_secrets(config_sources, display_sensitive, display_source, raw)
        else:
            self._filter_by_source(config_sources, display_source, self._get_secret_option)

        if not display_sensitive:
            # This ensures the ones from config file is hidden too
            # if they are not provided through env, cmd and secret
            hidden = "< hidden >"
            for section, key in self.sensitive_config_values:
                if config_sources.get(section):
                    if config_sources[section].get(key, None):
                        if display_source:
                            source = config_sources[section][key][1]
                            config_sources[section][key] = (hidden, source)
                        else:
                            config_sources[section][key] = hidden

        return config_sources

    def _include_secrets(
        self,
        config_sources: ConfigSourcesType,
        display_sensitive: bool,
        display_source: bool,
        raw: bool,
    ):
        for section, key in self.sensitive_config_values:
            value: str | None = self._get_secret_option_from_config_sources(config_sources, section, key)
            if value:
                if not display_sensitive:
                    value = "< hidden >"
                if display_source:
                    opt: str | tuple[str, str] = (value, "secret")
                elif raw:
                    opt = value.replace("%", "%%")
                else:
                    opt = value
                config_sources.setdefault(section, {}).update({key: opt})
                del config_sources[section][key + "_secret"]

    def _include_commands(
        self,
        config_sources: ConfigSourcesType,
        display_sensitive: bool,
        display_source: bool,
        raw: bool,
    ):
        for section, key in self.sensitive_config_values:
            opt = self._get_cmd_option_from_config_sources(config_sources, section, key)
            if not opt:
                continue
            opt_to_set: str | tuple[str, str] | None = opt
            if not display_sensitive:
                opt_to_set = "< hidden >"
            if display_source:
                opt_to_set = (str(opt_to_set), "cmd")
            elif raw:
                opt_to_set = str(opt_to_set).replace("%", "%%")
            if opt_to_set is not None:
                dict_to_update: dict[str, str | tuple[str, str]] = {key: opt_to_set}
                config_sources.setdefault(section, {}).update(dict_to_update)
                del config_sources[section][key + "_cmd"]

    def _include_envs(
        self,
        config_sources: ConfigSourcesType,
        display_sensitive: bool,
        display_source: bool,
        raw: bool,
    ):
        for env_var in [
            os_environment for os_environment in os.environ if os_environment.startswith(ENV_VAR_PREFIX)
        ]:
            try:
                _, section, key = env_var.split("__", 2)
                opt = self._get_env_var_option(section, key)
            except ValueError:
                continue
            if opt is None:
                log.warning("Ignoring unknown env var '%s'", env_var)
                continue
            if not display_sensitive and env_var != self._env_var_name("core", "unit_test_mode"):
                # Don't hide cmd/secret values here
                if not env_var.lower().endswith(("cmd", "secret")):
                    if (section, key) in self.sensitive_config_values:
                        opt = "< hidden >"
            elif raw:
                opt = opt.replace("%", "%%")
            if display_source:
                opt = (opt, "env var")

            section = section.lower()
            key = key.lower()
            config_sources.setdefault(section, {}).update({key: opt})

    def _filter_by_source(
        self,
        config_sources: ConfigSourcesType,
        display_source: bool,
        getter_func,
    ):
        """
        Delete default configs from current configuration.

        An OrderedDict of OrderedDicts, if it would conflict with special sensitive_config_values.

        This is necessary because bare configs take precedence over the command
        or secret key equivalents so if the current running config is
        materialized with Airflow defaults they in turn override user set
        command or secret key configs.

        :param config_sources: The current configuration to operate on
        :param display_source: If False, configuration options contain raw
            values. If True, options are a tuple of (option_value, source).
            Source is either 'airflow.cfg', 'default', 'env var', or 'cmd'.
        :param getter_func: A callback function that gets the user configured
            override value for a particular sensitive_config_values config.
        :return: None, the given config_sources is filtered if necessary,
            otherwise untouched.
        """
        for section, key in self.sensitive_config_values:
            # Don't bother if we don't have section / key
            if section not in config_sources or key not in config_sources[section]:
                continue
            # Check that there is something to override defaults
            try:
                getter_opt = getter_func(section, key)
            except ValueError:
                continue
            if not getter_opt:
                continue
            # Check to see that there is a default value
            if self.get_default_value(section, key) is None:
                continue
            # Check to see if bare setting is the same as defaults
            if display_source:
                # when display_source = true, we know that the config_sources contains tuple
                opt, source = config_sources[section][key]  # type: ignore
            else:
                opt = config_sources[section][key]
            if opt == self.get_default_value(section, key):
                del config_sources[section][key]

    @staticmethod
    def _replace_config_with_display_sources(
        config_sources: ConfigSourcesType,
        configs: Iterable[tuple[str, ConfigParser]],
        configuration_description: dict[str, dict[str, Any]],
        display_source: bool,
        raw: bool,
        deprecated_options: dict[tuple[str, str], tuple[str, str, str]],
        include_env: bool,
        include_cmds: bool,
        include_secret: bool,
    ):
        for source_name, config in configs:
            sections = config.sections()
            for section in sections:
                AirflowConfigParser._replace_section_config_with_display_sources(
                    config,
                    config_sources,
                    configuration_description,
                    display_source,
                    raw,
                    section,
                    source_name,
                    deprecated_options,
                    configs,
                    include_env=include_env,
                    include_cmds=include_cmds,
                    include_secret=include_secret,
                )

    @staticmethod
    def _deprecated_value_is_set_in_config(
        deprecated_section: str,
        deprecated_key: str,
        configs: Iterable[tuple[str, ConfigParser]],
    ) -> bool:
        for config_type, config in configs:
            if config_type != "default":
                with contextlib.suppress(NoSectionError):
                    deprecated_section_array = config.items(section=deprecated_section, raw=True)
                    if any(key == deprecated_key for key, _ in deprecated_section_array):
                        return True
        else:
            return False

    @staticmethod
    def _deprecated_variable_is_set(deprecated_section: str, deprecated_key: str) -> bool:
        return (
            os.environ.get(f"{ENV_VAR_PREFIX}{deprecated_section.upper()}__{deprecated_key.upper()}")
            is not None
        )

    @staticmethod
    def _deprecated_command_is_set_in_config(
        deprecated_section: str,
        deprecated_key: str,
        configs: Iterable[tuple[str, ConfigParser]],
    ) -> bool:
        return AirflowConfigParser._deprecated_value_is_set_in_config(
            deprecated_section=deprecated_section, deprecated_key=deprecated_key + "_cmd", configs=configs
        )

    @staticmethod
    def _deprecated_variable_command_is_set(deprecated_section: str, deprecated_key: str) -> bool:
        return (
            os.environ.get(f"{ENV_VAR_PREFIX}{deprecated_section.upper()}__{deprecated_key.upper()}_CMD")
            is not None
        )

    @staticmethod
    def _deprecated_secret_is_set_in_config(
        deprecated_section: str,
        deprecated_key: str,
        configs: Iterable[tuple[str, ConfigParser]],
    ) -> bool:
        return AirflowConfigParser._deprecated_value_is_set_in_config(
            deprecated_section=deprecated_section, deprecated_key=deprecated_key + "_secret", configs=configs
        )

    @staticmethod
    def _deprecated_variable_secret_is_set(deprecated_section: str, deprecated_key: str) -> bool:
        return (
            os.environ.get(f"{ENV_VAR_PREFIX}{deprecated_section.upper()}__{deprecated_key.upper()}_SECRET")
            is not None
        )

    @contextmanager
    def suppress_future_warnings(self):
        suppress_future_warnings = self._suppress_future_warnings
        self._suppress_future_warnings = True
        yield self
        self._suppress_future_warnings = suppress_future_warnings

    @staticmethod
    def _replace_section_config_with_display_sources(
        config: ConfigParser,
        config_sources: ConfigSourcesType,
        configuration_description: dict[str, dict[str, Any]],
        display_source: bool,
        raw: bool,
        section: str,
        source_name: str,
        deprecated_options: dict[tuple[str, str], tuple[str, str, str]],
        configs: Iterable[tuple[str, ConfigParser]],
        include_env: bool,
        include_cmds: bool,
        include_secret: bool,
    ):
        sect = config_sources.setdefault(section, {})
        if isinstance(config, AirflowConfigParser):
            with config.suppress_future_warnings():
                items: Iterable[tuple[str, Any]] = config.items(section=section, raw=raw)
        else:
            items = config.items(section=section, raw=raw)
        for k, val in items:
            deprecated_section, deprecated_key, _ = deprecated_options.get((section, k), (None, None, None))
            if deprecated_section and deprecated_key:
                if source_name == "default":
                    # If deprecated entry has some non-default value set for any of the sources requested,
                    # We should NOT set default for the new entry (because it will override anything
                    # coming from the deprecated ones)
                    if AirflowConfigParser._deprecated_value_is_set_in_config(
                        deprecated_section, deprecated_key, configs
                    ):
                        continue
                    if include_env and AirflowConfigParser._deprecated_variable_is_set(
                        deprecated_section, deprecated_key
                    ):
                        continue
                    if include_cmds and (
                        AirflowConfigParser._deprecated_variable_command_is_set(
                            deprecated_section, deprecated_key
                        )
                        or AirflowConfigParser._deprecated_command_is_set_in_config(
                            deprecated_section, deprecated_key, configs
                        )
                    ):
                        continue
                    if include_secret and (
                        AirflowConfigParser._deprecated_variable_secret_is_set(
                            deprecated_section, deprecated_key
                        )
                        or AirflowConfigParser._deprecated_secret_is_set_in_config(
                            deprecated_section, deprecated_key, configs
                        )
                    ):
                        continue
            if display_source:
                updated_source_name = source_name
                if source_name == "default":
                    # defaults can come from other sources (default-<PROVIDER>) that should be used here
                    source_description_section = configuration_description.get(section, {})
                    source_description_key = source_description_section.get("options", {}).get(k, {})
                    if source_description_key is not None:
                        updated_source_name = source_description_key.get("source", source_name)
                sect[k] = (val, updated_source_name)
            else:
                sect[k] = val

    def load_test_config(self):
        """
        Use test configuration rather than the configuration coming from airflow defaults.

        When running tests we use special the unit_test configuration to avoid accidental modifications and
        different behaviours when running the tests. Values for those test configuration are stored in
        the "unit_tests.cfg" configuration file in the ``airflow/config_templates`` folder
        and you need to change values there if you want to make some specific configuration to be used
        """
        # We need those globals before we run "get_all_expansion_variables" because this is where
        # the variables are expanded from in the configuration
        global FERNET_KEY, AIRFLOW_HOME, JWT_SECRET_KEY
        from cryptography.fernet import Fernet

        unit_test_config_file = pathlib.Path(__file__).parent / "config_templates" / "unit_tests.cfg"
        unit_test_config = unit_test_config_file.read_text()
        self.remove_all_read_configurations()
        with StringIO(unit_test_config) as test_config_file:
            self.read_file(test_config_file)
        # set fernet key to a random value
        FERNET_KEY = Fernet.generate_key().decode()
        JWT_SECRET_KEY = b64encode(os.urandom(16)).decode("utf-8")
        self.expand_all_configuration_values()
        log.info("Unit test configuration loaded from 'config_unit_tests.cfg'")

    def expand_all_configuration_values(self):
        """Expand all configuration values using global and local variables defined in this module."""
        all_vars = get_all_expansion_variables()
        for section in self.sections():
            for key, value in self.items(section):
                if value is not None:
                    if self.has_option(section, key):
                        self.remove_option(section, key)
                    if self.is_template(section, key) or not isinstance(value, str):
                        self.set(section, key, value)
                    else:
                        self.set(section, key, value.format(**all_vars))

    def remove_all_read_configurations(self):
        """Remove all read configurations, leaving only default values in the config."""
        for section in self.sections():
            self.remove_section(section)

    @property
    def providers_configuration_loaded(self) -> bool:
        """Checks if providers have been loaded."""
        return self._providers_configuration_loaded

    def load_providers_configuration(self):
        """
        Load configuration for providers.

        This should be done after initial configuration have been performed. Initializing and discovering
        providers is an expensive operation and cannot be performed when we load configuration for the first
        time when airflow starts, because we initialize configuration very early, during importing of the
        `airflow` package and the module is not yet ready to be used when it happens and until configuration
        and settings are loaded. Therefore, in order to reload provider configuration we need to additionally
        load provider - specific configuration.
        """
        log.debug("Loading providers configuration")
        from airflow.providers_manager import ProvidersManager

        self.restore_core_default_configuration()
        for provider, config in ProvidersManager().already_initialized_provider_configs:
            for provider_section, provider_section_content in config.items():
                provider_options = provider_section_content["options"]
                section_in_current_config = self.configuration_description.get(provider_section)
                if not section_in_current_config:
                    self.configuration_description[provider_section] = deepcopy(provider_section_content)
                    section_in_current_config = self.configuration_description.get(provider_section)
                    section_in_current_config["source"] = f"default-{provider}"
                    for option in provider_options:
                        section_in_current_config["options"][option]["source"] = f"default-{provider}"
                else:
                    section_source = section_in_current_config.get("source", "Airflow's core package").split(
                        "default-"
                    )[-1]
                    raise AirflowConfigException(
                        f"The provider {provider} is attempting to contribute "
                        f"configuration section {provider_section} that "
                        f"has already been added before. The source of it: {section_source}. "
                        "This is forbidden. A provider can only add new sections. It "
                        "cannot contribute options to existing sections or override other "
                        "provider's configuration.",
                        UserWarning,
                    )
        self._default_values = create_default_config_parser(self.configuration_description)
        # sensitive_config_values needs to be refreshed here. This is a cached_property, so we can delete
        # the cached values, and it will be refreshed on next access.
        with contextlib.suppress(AttributeError):
            # no problem if cache is not set yet
            del self.sensitive_config_values
        self._providers_configuration_loaded = True

    @staticmethod
    def _warn_deprecate(
        section: str, key: str, deprecated_section: str, deprecated_name: str, extra_stacklevel: int
    ):
        if section == deprecated_section:
            warnings.warn(
                f"The {deprecated_name} option in [{section}] has been renamed to {key} - "
                f"the old setting has been used, but please update your config.",
                DeprecationWarning,
                stacklevel=4 + extra_stacklevel,
            )
        else:
            warnings.warn(
                f"The {deprecated_name} option in [{deprecated_section}] has been moved to the {key} option "
                f"in [{section}] - the old setting has been used, but please update your config.",
                DeprecationWarning,
                stacklevel=4 + extra_stacklevel,
            )

    def __getstate__(self) -> dict[str, Any]:
        """Return the state of the object as a dictionary for pickling."""
        return {
            name: getattr(self, name)
            for name in [
                "_sections",
                "is_validated",
                "configuration_description",
                "upgraded_values",
                "_default_values",
            ]
        }

    def __setstate__(self, state) -> None:
        """Restore the state of the object from a dictionary representation."""
        self.__init__()  # type: ignore[misc]
        config = state.pop("_sections")
        self.read_dict(config)
        self.__dict__.update(state)


def get_airflow_home() -> str:
    """Get path to Airflow Home."""
    return expand_env_var(os.environ.get("AIRFLOW_HOME", "~/airflow"))


def get_airflow_config(airflow_home: str) -> str:
    """Get Path to airflow.cfg path."""
    airflow_config_var = os.environ.get("AIRFLOW_CONFIG")
    if airflow_config_var is None:
        return os.path.join(airflow_home, "airflow.cfg")
    return expand_env_var(airflow_config_var)


def get_all_expansion_variables() -> dict[str, Any]:
    return {k: v for d in [globals(), locals()] for k, v in d.items() if not k.startswith("_")}


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def create_default_config_parser(configuration_description: dict[str, dict[str, Any]]) -> ConfigParser:
    """
    Create default config parser based on configuration description.

    It creates ConfigParser with all default values retrieved from the configuration description and
    expands all the variables from the global and local variables defined in this module.

    :param configuration_description: configuration description - retrieved from config.yaml files
        following the schema defined in "config.yml.schema.json" in the config_templates folder.
    :return: Default Config Parser that can be used to read configuration values from.
    """
    parser = ConfigParser()
    all_vars = get_all_expansion_variables()
    for section, section_desc in configuration_description.items():
        parser.add_section(section)
        options = section_desc["options"]
        for key in options:
            default_value = options[key]["default"]
            is_template = options[key].get("is_template", False)
            if default_value is not None:
                if is_template or not isinstance(default_value, str):
                    parser.set(section, key, default_value)
                else:
                    parser.set(section, key, default_value.format(**all_vars))
    return parser


def create_provider_config_fallback_defaults() -> ConfigParser:
    """
    Create fallback defaults.

    This parser contains provider defaults for Airflow configuration, containing fallback default values
    that might be needed when provider classes are being imported - before provider's configuration
    is loaded.

    Unfortunately airflow currently performs a lot of stuff during importing and some of that might lead
    to retrieving provider configuration before the defaults for the provider are loaded.

    Those are only defaults, so if you have "real" values configured in your configuration (.cfg file or
    environment variables) those will be used as usual.

    NOTE!! Do NOT attempt to remove those default fallbacks thinking that they are unnecessary duplication,
    at least not until we fix the way how airflow imports "do stuff". This is unlikely to succeed.

    You've been warned!
    """
    config_parser = ConfigParser()
    config_parser.read(_default_config_file_path("provider_config_fallback_defaults.cfg"))
    return config_parser


def write_default_airflow_configuration_if_needed() -> AirflowConfigParser:
    global FERNET_KEY, JWT_SECRET_KEY
    airflow_config = pathlib.Path(AIRFLOW_CONFIG)
    if airflow_config.is_dir():
        msg = (
            "Airflow config expected to be a path to the configuration file, "
            f"but got a directory {airflow_config.__fspath__()!r}."
        )
        raise IsADirectoryError(msg)
    if not airflow_config.exists():
        log.debug("Creating new Airflow config file in: %s", airflow_config.__fspath__())
        config_directory = airflow_config.parent
        if not config_directory.exists():
            if not config_directory.is_relative_to(AIRFLOW_HOME):
                msg = (
                    f"Config directory {config_directory.__fspath__()!r} not exists "
                    f"and it is not relative to AIRFLOW_HOME {AIRFLOW_HOME!r}. "
                    "Please create this directory first."
                )
                raise FileNotFoundError(msg) from None
            log.debug("Create directory %r for Airflow config", config_directory.__fspath__())
            config_directory.mkdir(parents=True, exist_ok=True)
        if conf.get("core", "fernet_key", fallback=None) in (None, ""):
            # We know that FERNET_KEY is not set, so we can generate it, set as global key
            # and also write it to the config file so that same key will be used next time
            FERNET_KEY = _generate_fernet_key()
            conf.configuration_description["core"]["options"]["fernet_key"]["default"] = FERNET_KEY

        JWT_SECRET_KEY = b64encode(os.urandom(16)).decode("utf-8")
        conf.configuration_description["api_auth"]["options"]["jwt_secret"]["default"] = JWT_SECRET_KEY
        pathlib.Path(airflow_config.__fspath__()).touch()
        make_group_other_inaccessible(airflow_config.__fspath__())
        with open(airflow_config, "w") as file:
            conf.write(
                file,
                include_sources=False,
                include_env_vars=True,
                include_providers=True,
                extra_spacing=True,
                only_defaults=True,
            )
    return conf


def load_standard_airflow_configuration(airflow_config_parser: AirflowConfigParser):
    """
    Load standard airflow configuration.

    In case it finds that the configuration file is missing, it will create it and write the default
    configuration values there, based on defaults passed, and will add the comments and examples
    from the default configuration.

    :param airflow_config_parser: parser to which the configuration will be loaded

    """
    global AIRFLOW_HOME
    log.info("Reading the config from %s", AIRFLOW_CONFIG)
    airflow_config_parser.read(AIRFLOW_CONFIG)
    if airflow_config_parser.has_option("core", "AIRFLOW_HOME"):
        msg = (
            "Specifying both AIRFLOW_HOME environment variable and airflow_home "
            "in the config file is deprecated. Please use only the AIRFLOW_HOME "
            "environment variable and remove the config file entry."
        )
        if "AIRFLOW_HOME" in os.environ:
            warnings.warn(msg, category=DeprecationWarning, stacklevel=1)
        elif airflow_config_parser.get("core", "airflow_home") == AIRFLOW_HOME:
            warnings.warn(
                "Specifying airflow_home in the config file is deprecated. As you "
                "have left it at the default value you should remove the setting "
                "from your airflow.cfg and suffer no change in behaviour.",
                category=DeprecationWarning,
                stacklevel=1,
            )
        else:
            # there
            AIRFLOW_HOME = airflow_config_parser.get("core", "airflow_home")
            warnings.warn(msg, category=DeprecationWarning, stacklevel=1)


def initialize_config() -> AirflowConfigParser:
    """
    Load the Airflow config files.

    Called for you automatically as part of the Airflow boot process.
    """
    airflow_config_parser = AirflowConfigParser()
    if airflow_config_parser.getboolean("core", "unit_test_mode"):
        airflow_config_parser.load_test_config()
    else:
        load_standard_airflow_configuration(airflow_config_parser)
        # If the user set unit_test_mode in the airflow.cfg, we still
        # want to respect that and then load the default unit test configuration
        # file on top of it.
        if airflow_config_parser.getboolean("core", "unit_test_mode"):
            airflow_config_parser.load_test_config()
    return airflow_config_parser


def make_group_other_inaccessible(file_path: str):
    try:
        permissions = os.stat(file_path)
        os.chmod(file_path, permissions.st_mode & (stat.S_IRUSR | stat.S_IWUSR))
    except Exception as e:
        log.warning(
            "Could not change permissions of config file to be group/other inaccessible. "
            "Continuing with original permissions: %s",
            e,
        )


def ensure_secrets_loaded(
    default_backends: list[str] = DEFAULT_SECRETS_SEARCH_PATH,
) -> list[BaseSecretsBackend]:
    """
    Ensure that all secrets backends are loaded.

    If the secrets_backend_list contains only 2 default backends, reload it.
    """
    # Check if the secrets_backend_list contains only 2 default backends.

    # Check if we are loading the backends for worker too by checking if the default_backends is equal
    # to DEFAULT_SECRETS_SEARCH_PATH.
    if len(secrets_backend_list) == 2 or default_backends != DEFAULT_SECRETS_SEARCH_PATH:
        return initialize_secrets_backends(default_backends=default_backends)
    return secrets_backend_list


def get_custom_secret_backend(worker_mode: bool = False) -> BaseSecretsBackend | None:
    """
    Get Secret Backend if defined in airflow.cfg.

    Conditionally selects the section, key and kwargs key based on whether it is called from worker or not.
    """
    section = "workers" if worker_mode else "secrets"
    key = "secrets_backend" if worker_mode else "backend"
    kwargs_key = "secrets_backend_kwargs" if worker_mode else "backend_kwargs"

    secrets_backend_cls = conf.getimport(section=section, key=key)

    if not secrets_backend_cls:
        if worker_mode:
            # if we find no secrets backend for worker, return that of secrets backend
            secrets_backend_cls = conf.getimport(section="secrets", key="backend")
            if not secrets_backend_cls:
                return None
            # When falling back to secrets backend, use its kwargs
            kwargs_key = "backend_kwargs"
            section = "secrets"
        else:
            return None

    try:
        backend_kwargs = conf.getjson(section=section, key=kwargs_key)
        if not backend_kwargs:
            backend_kwargs = {}
        elif not isinstance(backend_kwargs, dict):
            raise ValueError("not a dict")
    except AirflowConfigException:
        log.warning("Failed to parse [%s] %s as JSON, defaulting to no kwargs.", section, kwargs_key)
        backend_kwargs = {}
    except ValueError:
        log.warning("Failed to parse [%s] %s into a dict, defaulting to no kwargs.", section, kwargs_key)
        backend_kwargs = {}

    return secrets_backend_cls(**backend_kwargs)


def initialize_secrets_backends(
    default_backends: list[str] = DEFAULT_SECRETS_SEARCH_PATH,
) -> list[BaseSecretsBackend]:
    """
    Initialize secrets backend.

    * import secrets backend classes
    * instantiate them and return them in a list
    """
    backend_list = []
    worker_mode = False
    if default_backends != DEFAULT_SECRETS_SEARCH_PATH:
        worker_mode = True

    custom_secret_backend = get_custom_secret_backend(worker_mode)

    if custom_secret_backend is not None:
        backend_list.append(custom_secret_backend)

    for class_name in default_backends:
        secrets_backend_cls = import_string(class_name)
        backend_list.append(secrets_backend_cls())

    return backend_list


def initialize_auth_manager() -> BaseAuthManager:
    """
    Initialize auth manager.

    * import user manager class
    * instantiate it and return it
    """
    auth_manager_cls = conf.getimport(section="core", key="auth_manager")

    if not auth_manager_cls:
        raise AirflowConfigException(
            "No auth manager defined in the config. Please specify one using section/key [core/auth_manager]."
        )

    return auth_manager_cls()


# Setting AIRFLOW_HOME and AIRFLOW_CONFIG from environment variables, using
# "~/airflow" and "$AIRFLOW_HOME/airflow.cfg" respectively as defaults.
AIRFLOW_HOME = get_airflow_home()
AIRFLOW_CONFIG = get_airflow_config(AIRFLOW_HOME)

# Set up dags folder for unit tests
# this directory won't exist if users install via pip
_TEST_DAGS_FOLDER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "tests", "dags"
)
if os.path.exists(_TEST_DAGS_FOLDER):
    TEST_DAGS_FOLDER = _TEST_DAGS_FOLDER
else:
    TEST_DAGS_FOLDER = os.path.join(AIRFLOW_HOME, "dags")

# Set up plugins folder for unit tests
_TEST_PLUGINS_FOLDER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "tests", "plugins"
)
if os.path.exists(_TEST_PLUGINS_FOLDER):
    TEST_PLUGINS_FOLDER = _TEST_PLUGINS_FOLDER
else:
    TEST_PLUGINS_FOLDER = os.path.join(AIRFLOW_HOME, "plugins")

SECRET_KEY = b64encode(os.urandom(16)).decode("utf-8")
FERNET_KEY = ""  # Set only if needed when generating a new file
JWT_SECRET_KEY = ""

conf: AirflowConfigParser = initialize_config()
secrets_backend_list = initialize_secrets_backends()
conf.validate()
