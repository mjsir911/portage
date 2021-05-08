# Copyright 2010-2021 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

__all__ = (
	'LocationsManager',
)

import io
import warnings
from pathlib import Path
from typing import Optional
import os as _os
import shlex

import portage
from portage import os, eapi_is_supported, _encodings, is_relative_to
from portage.const import CUSTOM_PROFILE_PATH, GLOBAL_CONFIG_PATH, \
	PROFILE_PATH, USER_CONFIG_PATH
from portage.eapi import eapi_allows_directories_on_profile_level_and_repository_level
from portage.exception import DirectoryNotFound, InvalidLocation, ParseError
from portage.localization import _
from portage.util import ensure_dirs, grabfile, \
	normalize_path, read_corresponding_eapi_file, writemsg
from portage.util._path import exists_raise_eaccess, isdir_raise_eaccess
from portage.repository.config import parse_layout_conf, \
	_portage1_profiles_allow_directories, _profile_node


_PORTAGE1_DIRECTORIES = frozenset([
	'package.mask', 'package.provided',
	'package.use', 'package.use.mask', 'package.use.force',
	'use.mask', 'use.force'])

_allow_parent_colon = frozenset(
	["portage-2"])

class LocationsManager:

	def __init__(self, config_root: Optional[Path] = None,
		eprefix: Optional[Path] = None,
		config_profile_path: Optional[Path] = None,
		local_config=True,
		target_root: Optional[Path] = None,
		sysroot: Optional[Path] = None):
		self.user_profile_dir = None
		self._local_repo_conf_path = None
		self.eprefix = eprefix
		self.config_root = config_root
		self.target_root = target_root
		self.sysroot = sysroot
		self._user_config = local_config

		if self.eprefix is None:
			self.eprefix = portage.const.EPREFIX
		elif self.eprefix:
			self.eprefix = normalize_path(self.eprefix)
			if self.eprefix == os.sep:
				self.eprefix = ""

		if self.config_root is None:
			self.config_root = portage.const.EPREFIX

		self.config_root = normalize_path(self.config_root.resolve())

		self._check_var_directory("PORTAGE_CONFIGROOT", self.config_root)
		self.abs_user_config = self.config_root / USER_CONFIG_PATH
		self.config_profile_path = config_profile_path

		if self.sysroot is None:
			self.sysroot = "/"
		else:
			self.sysroot = normalize_path(Path(self.sysroot or os.sep).absolute())

		self.esysroot = self.sysroot / self.eprefix.relative_to(os.sep)

		# TODO: Set this via the constructor using
		# PORTAGE_OVERRIDE_EPREFIX.
		self.broot = portage.const.EPREFIX

	def load_profiles(self, repositories, known_repository_paths):
		known_repository_paths = set(Path(_os.path.realpath(x))
			for x in known_repository_paths)

		known_repos = []
		for x in known_repository_paths:
			try:
				repo = repositories.get_repo_for_location(x)
			except KeyError:
				layout_data = parse_layout_conf(x)[0]
			else:
				layout_data = {
					"profile-formats": repo.profile_formats,
					"profile_eapi_when_unspecified": repo.eapi
				}
			# force a trailing '/' for ease of doing startswith checks
			known_repos.append((x, layout_data))
		known_repos = tuple(known_repos)

		if self.config_profile_path is None:
			deprecated_profile_path = self.config_root / 'etc' / 'make.profile'
			self.config_profile_path = self.config_root / PROFILE_PATH
			if isdir_raise_eaccess(self.config_profile_path):
				self.profile_path = self.config_profile_path
				if isdir_raise_eaccess(deprecated_profile_path) and not \
					self.profile_path.samefile(deprecated_profile_path):
					# Don't warn if they refer to the same path, since
					# that can be used for backward compatibility with
					# old software.
					writemsg("!!! %s\n" %
						_("Found 2 make.profile dirs: "
						"using '%s', ignoring '%s'") %
						(self.profile_path, deprecated_profile_path),
						noiselevel=-1)
			else:
				self.config_profile_path = deprecated_profile_path
				if isdir_raise_eaccess(self.config_profile_path):
					self.profile_path = self.config_profile_path
				else:
					self.profile_path = None
		else:
			# NOTE: repoman may pass in an empty string
			# here, in order to create an empty profile
			# for checking dependencies of packages with
			# empty KEYWORDS.
			self.profile_path = self.config_profile_path


		# The symlink might not exist or might not be a symlink.
		self.profiles = []
		self.profiles_complex = []
		if self.profile_path:
			try:
				self._addProfile(Path(_os.path.realpath(self.profile_path)),
					repositories, known_repos, ())
			except ParseError as e:
				if not portage._sync_mode:
					writemsg(_("!!! Unable to parse profile: '%s'\n") % self.profile_path, noiselevel=-1)
					writemsg("!!! ParseError: %s\n" % str(e), noiselevel=-1)
				self.profiles = []
				self.profiles_complex = []

		if self._user_config and self.profiles:
			custom_prof = self.config_root / CUSTOM_PROFILE_PATH
			if custom_prof.exists():
				# For read_corresponding_eapi_file, specify default=None
				# in order to allow things like wildcard atoms when
				# is no explicit EAPI setting.
				self.user_profile_dir = custom_prof
				self.profiles.append(custom_prof)
				self.profiles_complex.append(
					_profile_node(custom_prof, True, True,
					('profile-bashrcs', 'profile-set'),
					read_corresponding_eapi_file(
					custom_prof, default=None),
					True,
					show_deprecated_warning=False,
				))
			del custom_prof

		self.profiles = tuple(self.profiles)
		self.profiles_complex = tuple(self.profiles_complex)

	def _check_var_directory(self, varname, var):
		if not isdir_raise_eaccess(var):
			writemsg(_("!!! Error: %s='%s' is not a directory. "
				"Please correct this.\n") % (varname, var),
				noiselevel=-1)
			raise DirectoryNotFound(str(var))

	def _addProfile(self, currentPath: Path, repositories, known_repos, previous_repos):
		current_abs_path = currentPath.resolve()
		allow_directories = True
		allow_parent_colon = True
		repo_loc = None
		compat_mode = False
		current_formats = ()
		eapi = None

		intersecting_repos = tuple(x for x in known_repos
			if is_relative_to(current_abs_path, x[0]))
		if intersecting_repos:
			# Handle nested repositories. The longest path
			# will be the correct one.
			repo_loc, layout_data = max(intersecting_repos,
				key=lambda x:len(x[0].parts))
			eapi = layout_data.get("profile_eapi_when_unspecified")

		eapi_file = currentPath / "eapi"
		eapi = eapi or "0"
		f = None
		try:
			f = eapi_file.open(
				mode='r', encoding=_encodings['content'], errors='replace')
			eapi = f.readline().strip()
		except IOError:
			pass
		else:
			if not eapi_is_supported(eapi):
				raise ParseError(_(
					"Profile contains unsupported "
					"EAPI '%s': '%s'") % \
					(eapi, os.path.realpath(eapi_file),))
		finally:
			if f is not None:
				f.close()

		if intersecting_repos:
			allow_directories = eapi_allows_directories_on_profile_level_and_repository_level(eapi) or \
				any(x in _portage1_profiles_allow_directories for x in layout_data['profile-formats'])
			compat_mode = not eapi_allows_directories_on_profile_level_and_repository_level(eapi) and \
				layout_data['profile-formats'] == ('portage-1-compat',)
			allow_parent_colon = any(x in _allow_parent_colon
				for x in layout_data['profile-formats'])
			current_formats = tuple(layout_data['profile-formats'])

		# According to PMS, a deprecated profile warning is not inherited. Since
		# the current profile node may have been inherited by a user profile
		# node, the deprecation warning may be relevant even if it is not a
		# top-level profile node. Therefore, consider the deprecated warning
		# to be irrelevant when the current profile node belongs to the same
		# repo as the previous profile node.
		show_deprecated_warning = \
			tuple(x[0] for x in previous_repos) != tuple(x[0] for x in intersecting_repos)

		if compat_mode:
			offenders = _PORTAGE1_DIRECTORIES.intersection(_os.listdir(currentPath))
			offenders = sorted(x for x in offenders
				if (currentPath / x).is_dir())
			if offenders:
				warnings.warn(_(
					"\nThe selected profile is implicitly using the 'portage-1' format:\n"
					"\tprofile = %(profile_path)s\n"
					"But this repository is not using that format:\n"
					"\trepo = %(repo_name)s\n"
					"This will break in the future.  Please convert these dirs to files:\n"
					"\t%(files)s\n"
					"Or, add this line to the repository's layout.conf:\n"
					"\tprofile-formats = portage-1")
					% dict(profile_path=currentPath, repo_name=repo_loc,
						files='\n\t'.join(offenders)))

		parentsFile = currentPath / "parent"
		if exists_raise_eaccess(parentsFile):
			parents: List[str] = grabfile(parentsFile)
			if not parents:
				raise ParseError(
					_("Empty parent file: '%s'") % parentsFile)
			for parentPath in parents:
				if not parentPath.startswith(os.sep) and allow_parent_colon:
					parentPath = self._expand_parent_colon(parentsFile,
						parentPath, repo_loc, repositories)
				else:
					parentPath = Path(parentPath)

				# NOTE: This Path.joinpath (/) call is intended to ignore
				# currentPath if parentPath is already absolute.
				parentPath = normalize_path(currentPath / parentPath)

				if parentPath.is_absolute() or repo_loc is None or \
					not is_relative_to(repo_loc, parentPath):
					# It seems that this parent may point outside
					# of the current repo, so realpath it.
					parentPath = parentPath.absolute()

				if exists_raise_eaccess(parentPath):
					self._addProfile(parentPath, repositories, known_repos, intersecting_repos)
				else:
					raise ParseError(
						_("Parent '%s' not found: '%s'") %  \
						(parentPath, parentsFile))

		self.profiles.append(currentPath)
		self.profiles_complex.append(
			_profile_node(currentPath, allow_directories, False,
				current_formats, eapi, 'build-id' in current_formats,
				show_deprecated_warning=show_deprecated_warning,
		))

	def _expand_parent_colon(self, parentsFile: Path, parentPath: str,
			repo_loc: Path, repositories):
		# if isinstance(parentPath, Path): breakpoint()
		colon = parentPath.find(":")
		if colon == -1:
			return Path(parentPath)

		if colon == 0:
			if repo_loc is None:
				raise ParseError(
					_("Parent '%s' not found: '%s'") %  \
					(parentPath, parentsFile))
			else:
				parentPath = normalize_path(os.path.join(
					repo_loc, 'profiles', parentPath[colon+1:]))
		else:
			p_repo_name = parentPath[:colon]
			try:
				p_repo_loc = repositories.get_location_for_name(p_repo_name)
			except KeyError:
				raise ParseError(
					_("Parent '%s' not found: '%s'") %  \
					(parentPath, parentsFile))
			else:
				parentPath = normalize_path(os.path.join(
					p_repo_loc, 'profiles', parentPath[colon+1:]))

		return parentPath

	def set_root_override(self, root_overwrite: Optional[Path] = None):
		# Allow ROOT setting to come from make.conf if it's not overridden
		# by the constructor argument (from the calling environment).
		if self.target_root is None and root_overwrite is not None:
			self.target_root = root_overwrite
			if not self.target_root.strip():
				self.target_root = None
		self.target_root = self.target_root or Path(os.sep)

		self.target_root = normalize_path(self.target_root.resolve())

		if self.sysroot != "/" and self.sysroot != self.target_root:
			writemsg(_("!!! Error: SYSROOT (currently %s) must "
				"equal / or ROOT (currently %s).\n") %
				(self.sysroot, self.target_root),
				noiselevel=-1)
			raise InvalidLocation(self.sysroot)

		ensure_dirs(self.target_root)
		self._check_var_directory("ROOT", self.target_root)

		self.eroot = self.target_root / self.eprefix.relative_to(os.sep)

		self.global_config_path = GLOBAL_CONFIG_PATH
		if portage.const.EPREFIX:
			self.global_config_path = (portage.const.EPREFIX /
				GLOBAL_CONFIG_PATH.relative_to(os.sep))

	def set_port_dirs(self, portdir: Optional[Path], portdir_overlay: Optional):
		self.portdir = portdir
		self.portdir_overlay = portdir_overlay
		if self.portdir_overlay is None:
			self.portdir_overlay = ""

		self.overlay_profiles = []
		for ov in shlex.split(self.portdir_overlay):
			ov = normalize_path(Path(ov))
			profiles_dir = ov / "profiles"
			if profiles_dir.is_dir():
				self.overlay_profiles.append(profiles_dir)

		self.profile_locations = ([portdir / "profiles"] if portdir else []) \
			+ self.overlay_profiles
		self.profile_and_user_locations = self.profile_locations[:]
		if self._user_config:
			self.profile_and_user_locations.append(self.abs_user_config)

		self.profile_locations = tuple(self.profile_locations)
		self.profile_and_user_locations = tuple(self.profile_and_user_locations)
