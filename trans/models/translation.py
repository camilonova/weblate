# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2013 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <http://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from django.db import models
from django.contrib.auth.models import User
from weblate import appsettings
from django.db.models import Q
from django.utils.translation import ugettext as _
from django.utils.safestring import mark_safe
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.utils import timezone
from django.core.urlresolvers import reverse
import os.path
import logging
import git
from translate.storage.lisa import LISAfile
from translate.storage import poheader
from datetime import datetime, timedelta

import weblate
from lang.models import Language
from trans.formats import ttkit
from trans.checks import CHECKS
from trans.models.subproject import SubProject
from trans.models.project import Project
from trans.util import (
    msg_checksum, get_source, get_target, get_context,
    is_translated, is_translatable, get_user_display,
    get_site_url, sleep_while_git_locked,
)

logger = logging.getLogger('weblate')


class TranslationManager(models.Manager):
    def update_from_blob(self, subproject, code, path, force=False,
                         request=None):
        '''
        Parses translation meta info and creates/updates translation object.
        '''
        lang = Language.objects.auto_get_or_create(code=code)
        translation, dummy = self.get_or_create(
            language=lang,
            language_code=code,
            subproject=subproject
        )
        if translation.filename != path:
            force = True
            translation.filename = path
        translation.update_from_blob(force, request=request)

        return translation

    def enabled(self):
        '''
        Filters enabled translations.
        '''
        return self.filter(enabled=True)

    def all_acl(self, user):
        '''
        Returns list of projects user is allowed to access.
        '''
        projects, filtered = Project.objects.get_acl_status(user)
        if not filtered:
            return self.all()
        return self.filter(subproject__project__in=projects)


class Translation(models.Model):
    subproject = models.ForeignKey(SubProject)
    language = models.ForeignKey(Language)
    revision = models.CharField(max_length=100, default='', blank=True)
    filename = models.CharField(max_length=200)

    translated = models.IntegerField(default=0, db_index=True)
    fuzzy = models.IntegerField(default=0, db_index=True)
    total = models.IntegerField(default=0, db_index=True)

    enabled = models.BooleanField(default=True, db_index=True)

    language_code = models.CharField(max_length=20, default='')

    lock_user = models.ForeignKey(User, null=True, blank=True, default=None)
    lock_time = models.DateTimeField(default=datetime.now)

    objects = TranslationManager()

    class Meta:
        ordering = ['language__name']
        permissions = (
            ('upload_translation', "Can upload translation"),
            ('overwrite_translation', "Can overwrite with translation upload"),
            ('author_translation', "Can define author of translation upload"),
            ('commit_translation', "Can force commiting of translation"),
            ('update_translation', "Can update translation from"),
            ('push_translation', "Can push translations to remote"),
            ('reset_translation', "Can reset translations to match remote"),
            ('automatic_translation', "Can do automatic translation"),
            ('lock_translation', "Can lock whole translation project"),
        )
        app_label = 'trans'

    def __init__(self, *args, **kwargs):
        '''
        Constructor to initialize some cache properties.
        '''
        super(Translation, self).__init__(*args, **kwargs)
        self._store = None

    def has_acl(self, user):
        '''
        Checks whether current user is allowed to access this
        subproject.
        '''
        return self.subproject.project.has_acl(user)

    def check_acl(self, request):
        '''
        Raises an error if user is not allowed to acces s this project.
        '''
        self.subproject.project.check_acl(request)

    def clean(self):
        '''
        Validates that filename exists and can be opened using ttkit.
        '''
        if not os.path.exists(self.get_filename()):
            raise ValidationError(
                _(
                    'Filename %s not found in repository! To add new '
                    'translation, add language file into repository.'
                ) %
                self.filename
            )
        try:
            self.get_store()
        except ValueError:
            raise ValidationError(
                _('Format of %s could not be recognized.') %
                self.filename
            )
        except Exception as e:
            raise ValidationError(
                _('Failed to parse file %(file)s: %(error)s') % {
                    'file': self.filename,
                    'error': str(e)
                }
            )

    def get_fuzzy_percent(self):
        if self.total == 0:
            return 0
        return round(self.fuzzy * 100.0 / self.total, 1)

    def get_translated_percent(self):
        if self.total == 0:
            return 0
        return round(self.translated * 100.0 / self.total, 1)

    def get_lock_user_display(self):
        '''
        Returns formatted lock user.
        '''
        return get_user_display(self.lock_user)

    def get_lock_display(self):
        return mark_safe(
            _('This translation is locked by %(user)s!') % {
                'user': self.get_lock_user_display(),
            }
        )

    def is_locked(self, request=None, multi=False):
        '''
        Check whether the translation is locked and
        possibly emits messages if request object is
        provided.
        '''

        prj_lock = self.subproject.locked
        usr_lock, own_lock = self.is_user_locked(request, True)

        # Calculate return value
        if multi:
            return (prj_lock, usr_lock, own_lock)
        else:
            return prj_lock or usr_lock

    def is_user_locked(self, request=None, multi=False):
        '''
        Checks whether there is valid user lock on this translation.
        '''
        # Any user?
        if self.lock_user is None:
            result = (False, False)

        # Is lock still valid?
        elif self.lock_time < datetime.now():
            # Clear the lock
            self.create_lock(None)

            result = (False, False)

        # Is current user the one who has locked?
        elif request is not None and self.lock_user == request.user:
            result = (False, True)

        else:
            result = (True, False)

        if multi:
            return result
        else:
            return result[0]

    def create_lock(self, user, explicit=False):
        '''
        Creates lock on translation.
        '''
        is_new = self.lock_user is None
        self.lock_user = user

        # Clean timestamp on unlock
        if user is None:
            self.lock_time = datetime.now()
            self.save()
            return

        self.update_lock_time(explicit, is_new)

    def update_lock_time(self, explicit=False, is_new=True):
        '''
        Sets lock timestamp.
        '''
        if explicit:
            seconds = appsettings.LOCK_TIME
        else:
            seconds = appsettings.AUTO_LOCK_TIME

        new_lock_time = datetime.now() + timedelta(seconds=seconds)

        if is_new or new_lock_time > self.lock_time:
            self.lock_time = new_lock_time

        self.save()

    def update_lock(self, request):
        '''
        Updates lock timestamp.
        '''
        # Update timestamp
        if self.is_user_locked():
            self.update_lock_time()
            return

        # Auto lock if we should
        if appsettings.AUTO_LOCK:
            self.create_lock(request.user)
            return

    def get_non_translated(self):
        return self.total - self.translated

    @models.permalink
    def get_absolute_url(self):
        return ('translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    def get_share_url(self):
        '''
        Returns absolute URL usable for sharing.
        '''
        return get_site_url(
            reverse(
                'engage-lang',
                kwargs={
                    'project': self.subproject.project.slug,
                    'lang': self.language.code
                }
            )
        )

    @models.permalink
    def get_commit_url(self):
        return ('commit_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_update_url(self):
        return ('update_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_push_url(self):
        return ('push_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_reset_url(self):
        return ('reset_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    def is_git_lockable(self):
        return False

    @models.permalink
    def get_lock_url(self):
        return ('lock_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_unlock_url(self):
        return ('unlock_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_download_url(self):
        return ('download_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_translate_url(self):
        return ('translate', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_source_review_url(self):
        return ('review_source', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
        })

    def __unicode__(self):
        return '%s - %s' % (
            self.subproject.__unicode__(),
            _(self.language.name)
        )

    def get_filename(self):
        '''
        Returns absolute filename.
        '''
        return os.path.join(self.subproject.get_path(), self.filename)

    def get_store(self):
        '''
        Returns ttkit storage object for a translation.
        '''
        if self._store is None:
            self._store = ttkit(
                self.get_filename(),
                self.subproject.file_format
            )
        return self._store

    def check_sync(self):
        '''
        Checks whether database is in sync with git and possibly does update.
        '''
        self.update_from_blob()

    def update_from_blob(self, force=False, request=None):
        '''
        Updates translation data from blob.
        '''
        from trans.models.unit import Unit
        from trans.models.unitdata import Check, Suggestion, Comment, Change
        blob_hash = self.get_git_blob_hash()

        # Check if we're not already up to date
        if self.revision != blob_hash:
            logger.info(
                'processing %s in %s, revision has changed',
                self.filename,
                self.subproject.__unicode__()
            )
        elif force:
            logger.info(
                'processing %s in %s, check forced',
                self.filename,
                self.subproject.__unicode__()
            )
        else:
            return

        oldunits = set(self.unit_set.all().values_list('id', flat=True))

        # Was there change?
        was_new = False
        # Position of current unit
        pos = 1
        # Load translation file
        store = self.get_store()
        # Load translation template
        template_store = self.subproject.get_template_store()
        if template_store is None:
            for unit in store.units:
                # We care only about translatable strings
                if not is_translatable(unit):
                    continue
                newunit, is_new = Unit.objects.update_from_unit(
                    self, unit, pos
                )
                was_new = was_new or (is_new and not newunit.translated)
                pos += 1
                try:
                    oldunits.remove(newunit.id)
                except:
                    pass
        else:
            for template_unit in template_store.units:
                # We care only about translatable strings
                if not is_translatable(template_unit):
                    continue
                unit = store.findid(template_unit.getid())
                newunit, is_new = Unit.objects.update_from_unit(
                    self, unit, pos, template=template_unit
                )
                was_new = was_new or (is_new and not newunit.translated)
                pos += 1
                try:
                    oldunits.remove(newunit.id)
                except:
                    pass

        # Delete not used units
        units_to_delete = Unit.objects.filter(
            translation=self, id__in=oldunits
        )
        # We need to resolve this now as otherwise list will become empty after
        # delete
        deleted_checksums = units_to_delete.values_list('checksum', flat=True)
        deleted_checksums = list(deleted_checksums)
        units_to_delete.delete()

        # Cleanup checks for deleted units
        for checksum in deleted_checksums:
            units = Unit.objects.filter(
                translation__language=self.language,
                translation__subproject__project=self.subproject.project,
                checksum=checksum
            )
            if not units.exists():
                # Last unit referencing to these checks
                Check.objects.filter(
                    project=self.subproject.project,
                    language=self.language,
                    checksum=checksum
                ).delete()
                # Delete suggestons referencing this unit
                Suggestion.objects.filter(
                    project=self.subproject.project,
                    language=self.language,
                    checksum=checksum
                ).delete()
                # Delete translation comments referencing this unit
                Comment.objects.filter(
                    project=self.subproject.project,
                    language=self.language,
                    checksum=checksum
                ).delete()
                # Check for other units with same source
                other_units = Unit.objects.filter(
                    translation__subproject__project=self.subproject.project,
                    checksum=checksum
                )
                if not other_units.exists():
                    # Delete source comments as well if this was last reference
                    Comment.objects.filter(
                        project=self.subproject.project,
                        language=None,
                        checksum=checksum
                    ).delete()
                    # Delete source checks as well if this was last reference
                    Check.objects.filter(
                        project=self.subproject.project,
                        language=None,
                        checksum=checksum
                    ).delete()
            else:
                # There are other units as well, but some checks
                # (eg. consistency) needs update now
                for unit in units:
                    unit.check()

        # Update revision and stats
        self.update_stats()

        # Store change entry
        if request is None:
            user = None
        else:
            user = request.user
        Change.objects.create(
            translation=self,
            action=Change.ACTION_UPDATE,
            user=user
        )

        # Notify subscribed users
        if was_new:
            from accounts.models import Profile
            subscriptions = Profile.objects.subscribed_new_string(
                self.subproject.project, self.language
            )
            for subscription in subscriptions:
                subscription.notify_new_string(self)

    @property
    def git_repo(self):
        return self.subproject.git_repo

    def get_last_remote_commit(self):
        return self.subproject.get_last_remote_commit()

    def do_update(self, request=None):
        return self.subproject.do_update(request)

    def do_push(self, request=None):
        return self.subproject.do_push(request)

    def do_reset(self, request=None):
        return self.subproject.do_reset(request)

    def can_push(self):
        return self.subproject.can_push()

    def get_git_blob_hash(self):
        '''
        Returns current Git blob hash for file.
        '''
        tree = self.git_repo.tree()
        ret = tree[self.filename].hexsha
        if self.subproject.has_template():
            ret += ','
            ret += tree[self.subproject.template].hexsha
        return ret

    def update_stats(self):
        '''
        Updates translation statistics.
        '''
        self.total = self.unit_set.count()
        self.fuzzy = self.unit_set.filter(fuzzy=True).count()
        self.translated = self.unit_set.filter(translated=True).count()
        self.save()
        self.store_hash()

    def store_hash(self):
        '''
        Stores current hash in database.
        '''
        blob_hash = self.get_git_blob_hash()
        self.revision = blob_hash
        self.save()

    def get_last_author(self, email=True):
        '''
        Returns last autor of change done in Weblate.
        '''
        from trans.models.unitdata import Change
        try:
            change = Change.objects.content().filter(translation=self)[0]
            return self.get_author_name(change.user, email)
        except IndexError:
            return None

    def get_last_change(self):
        '''
        Returns date of last change done in Weblate.
        '''
        from trans.models.unitdata import Change
        try:
            change = Change.objects.content().filter(translation=self)[0]
            return change.timestamp
        except IndexError:
            return None

    def commit_pending(self, author=None, skip_push=False):
        '''
        Commits any pending changes.
        '''
        # Get author of last changes
        last = self.get_last_author()

        # If it is same as current one, we don't have to commit
        if author == last or last is None:
            return

        # Commit changes
        self.git_commit(last, self.get_last_change(), True, True, skip_push)

    def get_author_name(self, user, email=True):
        '''
        Returns formatted author name with email.
        '''
        # Get full name from database
        full_name = user.get_full_name()

        # Use username if full name is empty
        if full_name == '':
            full_name = user.username

        # Add email if we are asked for it
        if not email:
            return full_name
        return '%s <%s>' % (full_name, user.email)

    def get_commit_message(self):
        '''
        Formats commit message based on project configuration.
        '''
        return self.subproject.project.commit_message % {
            'language': self.language_code,
            'language_name': self.language.name,
            'subproject': self.subproject.name,
            'project': self.subproject.project.name,
            'total': self.total,
            'fuzzy': self.fuzzy,
            'fuzzy_percent': self.get_fuzzy_percent(),
            'translated': self.translated,
            'translated_percent': self.get_translated_percent(),
        }

    def __configure_git(self, gitrepo, section, key, expected):
        '''
        Adjysts git config to ensure that section.key is set to expected.
        '''
        cnf = gitrepo.config_writer()
        try:
            # Get value and if it matches we're done
            value = cnf.get(section, key)
            if value == expected:
                return
        except:
            pass

        # Try to add section (might fail if it exists)
        try:
            cnf.add_section(section)
        except:
            pass
        # Update config
        cnf.set(section, key, expected)

    def __configure_committer(self, gitrepo):
        '''
        Wrapper for setting proper committer. As this can not be done by
        passing parameter, we need to check config on every commit.
        '''
        self.__configure_git(
            gitrepo,
            'user',
            'name',
            self.subproject.project.committer_name
        )
        self.__configure_git(
            gitrepo,
            'user',
            'email',
            self.subproject.project.committer_email
        )

    def __git_commit(self, gitrepo, author, timestamp, sync=False):
        '''
        Commits translation to git.
        '''
        # Check git config
        self.__configure_committer(gitrepo)

        # Format commit message
        msg = self.get_commit_message()

        # Do actual commit
        gitrepo.git.commit(
            self.filename,
            author=author.encode('utf-8'),
            date=timestamp.isoformat(),
            m=msg
        )

        # Optionally store updated hash
        if sync:
            self.store_hash()

    def git_needs_commit(self):
        '''
        Checks whether there are some not commited changes.
        '''
        status = self.git_repo.git.status('--porcelain', '--', self.filename)
        if status == '':
            # No changes to commit
            return False
        return True

    def git_needs_merge(self):
        return self.subproject.git_needs_merge()

    def git_needs_push(self):
        return self.subproject.git_needs_push()

    def git_commit(self, author, timestamp, force_commit=False, sync=False,
                   skip_push=False):
        '''
        Wrapper for commiting translation to git.

        force_commit forces commit with lazy commits enabled

        sync updates git hash stored within the translation (otherwise
        translation rescan will be needed)
        '''
        gitrepo = self.git_repo

        # Is there something for commit?
        if not self.git_needs_commit():
            return False

        # Can we delay commit?
        if not force_commit and appsettings.LAZY_COMMITS:
            logger.info(
                'Delaying commiting %s in %s as %s',
                self.filename,
                self,
                author
            )
            return False

        # Do actual commit with git lock
        logger.info('Commiting %s in %s as %s', self.filename, self, author)
        with self.subproject.get_git_lock():
            try:
                self.__git_commit(gitrepo, author, timestamp, sync)
            except git.GitCommandError:
                # There might be another attempt on commit in same time
                # so we will sleep a bit an retry
                sleep_while_git_locked()
                self.__git_commit(gitrepo, author, timestamp, sync)

        # Push if we should
        if self.subproject.project.push_on_commit and not skip_push:
            self.subproject.do_push(force_commit=False)

        return True

    def update_unit(self, unit, request):
        '''
        Updates backend file and unit.
        '''
        # Save with lock acquired
        with self.subproject.get_git_lock():

            store = self.get_store()
            src = unit.get_source_plurals()[0]
            need_save = False
            found = False
            add = False

            if self.subproject.has_template():
                # We search by ID when using template
                pounit = store.findid(unit.context)
                if pounit is not None:
                    found = True
                else:
                    # Need to create new unit based on template
                    template_store = self.subproject.get_template_store()
                    pounit = template_store.findid(unit.context)
                    add = True
                    found = pounit is not None
            else:
                # Find all units with same source
                found_units = store.findunits(src)
                if len(found_units) > 0:
                    for pounit in found_units:
                        # Does context match?
                        if pounit.getcontext() == unit.context:
                            # We should have only one match
                            found = True
                            break
                else:
                    # Fallback to manual find for value based files
                    for pounit in store.units:
                        if get_source(pounit) == src:
                            found = True
                            break

            # Bail out if we have not found anything
            if not found:
                return False, None

            # Detect changes
            if unit.target != get_target(pounit) or unit.fuzzy != pounit.isfuzzy():
                # Store translations
                if unit.is_plural():
                    pounit.settarget(unit.get_target_plurals())
                else:
                    pounit.settarget(unit.target)
                # Update fuzzy flag
                pounit.markfuzzy(unit.fuzzy)
                # Optionally add unit to translation file
                if add:
                    if isinstance(store, LISAfile):
                        # LISA based stores need to know this
                        store.addunit(pounit, new=True)
                    else:
                        store.addunit(pounit)
                # We need to update backend
                need_save = True

            # Save backend if there was a change
            if need_save:
                author = self.get_author_name(request.user)
                # Update po file header
                if hasattr(store, 'updateheader'):
                    po_revision_date = (
                        datetime.now().strftime('%Y-%m-%d %H:%M')
                        + poheader.tzstring()
                    )

                    # Update genric headers
                    store.updateheader(
                        add=True,
                        last_translator=author,
                        plural_forms=self.language.get_plural_form(),
                        language=self.language_code,
                        PO_Revision_Date=po_revision_date,
                        x_generator='Weblate %s' % weblate.VERSION
                    )

                    if self.subproject.project.set_translation_team:
                        # Store language team with link to website
                        store.updateheader(
                            language_team='%s <%s>' % (
                                self.language.name,
                                get_site_url(self.get_absolute_url()),
                            )
                        )
                        # Optionally store email for reporting bugs in source
                        report_source_bugs = self.subproject.report_source_bugs
                        if report_source_bugs != '':
                            store.updateheader(
                                report_msgid_bugs_to=report_source_bugs,
                            )
                # commit possible previous changes (by other author)
                self.commit_pending(author)
                # save translation changes
                store.save()
                # commit Git repo if needed
                self.git_commit(author, timezone.now(), sync=True)

        return need_save, pounit

    def get_source_checks(self):
        '''
        Returns list of failing source checks on current subproject.
        '''
        result = [('all', _('All strings'))]

        # All checks
        sourcechecks = self.unit_set.count_type('sourcechecks', self)
        if sourcechecks > 0:
            result.append((
                'sourcechecks',
                _('Strings with any failing checks (%d)') % sourcechecks
            ))

        # Process specific checks
        for check in CHECKS:
            if not CHECKS[check].source:
                continue
            cnt = self.unit_set.count_type(check, self)
            if cnt > 0:
                desc = CHECKS[check].description + (' (%d)' % cnt)
                result.append((check, desc))

        # Grab comments
        sourcecomments = self.unit_set.count_type('sourcecomments', self)
        if sourcecomments > 0:
            result.append((
                'sourcecomments',
                _('Strings with comments (%d)') % sourcecomments
            ))

        return result

    def get_translation_checks(self):
        '''
        Returns list of failing checks on current translation.
        '''
        result = [('all', _('All strings'))]

        # Untranslated strings
        nottranslated = self.unit_set.count_type('untranslated', self)
        if nottranslated > 0:
            result.append((
                'untranslated',
                _('Untranslated strings (%d)') % nottranslated
            ))

        # Fuzzy strings
        fuzzy = self.unit_set.count_type('fuzzy', self)
        if fuzzy > 0:
            result.append((
                'fuzzy',
                _('Fuzzy strings (%d)') % fuzzy
            ))

        # Translations with suggestions
        suggestions = self.unit_set.count_type('suggestions', self)
        if suggestions > 0:
            result.append((
                'suggestions',
                _('Strings with suggestions (%d)') % suggestions
            ))

        # All checks
        allchecks = self.unit_set.count_type('allchecks', self)
        if allchecks > 0:
            result.append((
                'allchecks',
                _('Strings with any failing checks (%d)') % allchecks
            ))

        # Process specific checks
        for check in CHECKS:
            if not CHECKS[check].target:
                continue
            cnt = self.unit_set.count_type(check, self)
            if cnt > 0:
                desc = CHECKS[check].description + (' (%d)' % cnt)
                result.append((check, desc))

        # Grab comments
        targetcomments = self.unit_set.count_type('targetcomments', self)
        if targetcomments > 0:
            result.append((
                'targetcomments',
                _('Strings with comments (%d)') % targetcomments
            ))

        return result

    def merge_store(self, author, store2, overwrite, merge_header, add_fuzzy):
        '''
        Merges ttkit store into current translation.
        '''
        # Merge with lock acquired
        with self.subproject.get_git_lock():

            store1 = self.get_store()
            store1.require_index()

            for unit2 in store2.units:
                # No translated -> skip
                if not is_translated(unit2):
                    continue

                # Optionally merge header
                if unit2.isheader():
                    if merge_header and isinstance(store1, poheader.poheader):
                        store1.mergeheaders(store2)
                    continue

                # Find unit by ID
                unit1 = store1.findid(unit2.getid())

                # Fallback to finding by source
                if unit1 is None:
                    unit1 = store1.findunit(unit2.source)

                # Unit not found, nothing to do
                if unit1 is None:
                    continue

                # Should we overwrite
                if not overwrite and unit1.istranslated():
                    continue

                # Actually update translation
                unit1.merge(unit2, overwrite=True, comments=False)

                # Handle
                if add_fuzzy:
                    unit1.markfuzzy()

            # Write to backend and commit
            self.commit_pending(author)
            store1.save()
            ret = self.git_commit(author, timezone.now(), True)
            self.check_sync()

        return ret

    def merge_suggestions(self, request, store):
        '''
        Merges contect of ttkit store as a suggestions.
        '''
        from trans.models.unitdata import Suggestion
        ret = False
        for unit in store.units:

            # Skip headers or not translated
            if unit.isheader() or not is_translated(unit):
                continue

            # Indicate something new
            ret = True

            # Calculate unit checksum
            src = get_source(unit)
            ctx = get_context(unit)
            checksum = msg_checksum(src, ctx)

            # Create suggestion objects.
            # We don't care about duplicates or non existing strings here
            # this is better handled in cleanup.
            Suggestion.objects.create(
                target=get_target(unit),
                checksum=checksum,
                language=self.language,
                project=self.subproject.project,
                user=request.user
            )

        # Invalidate cache if we've added something
        if ret:
            self.invalidate_cache('suggestions')

        return ret

    def merge_upload(self, request, fileobj, overwrite, author=None,
                     merge_header=True, method=''):
        '''
        Top level handler for file uploads.
        '''
        # Load backend file
        try:
            store = ttkit(fileobj)
        except:
            store = ttkit(fileobj, self.subproject.file_format)

        # Optionally set authorship
        if author is None:
            author = self.get_author_name(request.user)

        # List translations we should process
        translations = Translation.objects.filter(
            language=self.language,
            subproject__project=self.subproject.project
        )
        # Filter out those who don't want automatic update, but keep ourselves
        translations = translations.filter(
            Q(pk=self.pk) | Q(subproject__allow_translation_propagation=True)
        )

        ret = False

        if method in ('', 'fuzzy'):
            # Do actual merge
            for translation in translations:
                ret |= translation.merge_store(
                    author,
                    store,
                    overwrite,
                    merge_header,
                    (method == 'fuzzy')
                )
        else:
            # Add as sugestions
            ret = self.merge_suggestions(request, store)

        return ret

    def get_suggestions_count(self):
        '''
        Returns number of units with suggestions.
        '''
        return self.unit_set.count_type('suggestions', self)

    def get_failing_checks(self, check='allchecks'):
        '''
        Returns number of units with failing checks.

        By default for all checks or check type can be specified.
        '''
        return self.unit_set.count_type(check, self)

    def get_failing_checks_percent(self, check='allchecks'):
        '''
        Returns percentage of failed checks.
        '''
        if self.total == 0:
            return 0
        return round(self.get_failing_checks(check) * 100.0 / self.total, 1)

    def invalidate_cache(self, cache_type=None):
        '''
        Invalidates any cached stats.
        '''
        # Get parts of key cache
        slug = self.subproject.get_full_slug()
        code = self.language.code

        # Are we asked for specific cache key?
        if cache_type is None:
            keys = ['allchecks'] + list(CHECKS)
        else:
            keys = [cache_type]

        # Actually delete the cache
        for rqtype in keys:
            cache_key = 'counts-%s-%s-%s' % (slug, code, rqtype)
            cache.delete(cache_key)
