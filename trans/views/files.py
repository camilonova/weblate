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

from django.core.servers.basehttp import FileWrapper
from django.utils.translation import ugettext as _
from django.http import HttpResponse, HttpResponseRedirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required

from trans.forms import UploadForm, SimpleUploadForm, ExtraUploadForm
from trans.views.helper import get_translation

import logging
import os.path

logger = logging.getLogger('weblate')


# See https://code.djangoproject.com/ticket/6027
class FixedFileWrapper(FileWrapper):
    def __iter__(self):
        self.filelike.seek(0)
        return self


def download_translation(request, project, subproject, lang):
    obj = get_translation(request, project, subproject, lang)

    # Retrieve ttkit store to get extension and mime type
    store = obj.get_store()
    srcfilename = obj.get_filename()

    if store.Mimetypes is None:
        # Properties files do not expose mimetype
        mime = 'text/plain'
    else:
        mime = store.Mimetypes[0]

    if store.Extensions is None:
        # Typo in translate-toolkit 1.9, see
        # https://github.com/translate/translate/pull/10
        if hasattr(store, 'Exensions'):
            ext = store.Exensions[0]
        else:
            ext = 'txt'
    else:
        ext = store.Extensions[0]

    # Construct file name (do not use real filename as it is usually not
    # that useful)
    filename = '%s-%s-%s.%s' % (project, subproject, lang, ext)

    # Django wrapper for sending file
    wrapper = FixedFileWrapper(file(srcfilename))

    response = HttpResponse(wrapper, mimetype=mime)

    # Fill in response headers
    response['Content-Disposition'] = 'attachment; filename=%s' % filename
    response['Content-Length'] = os.path.getsize(srcfilename)

    return response


@login_required
@permission_required('trans.upload_translation')
def upload_translation(request, project, subproject, lang):
    '''
    Handling of translation uploads.
    '''
    obj = get_translation(request, project, subproject, lang)

    # Check method and lock
    if obj.is_locked(request) or request.method != 'POST':
        messages.error(request, _('Access denied.'))
        return HttpResponseRedirect(obj.get_absolute_url())

    # Get correct form handler based on permissions
    if request.user.has_perm('trans.author_translation'):
        form = ExtraUploadForm(request.POST, request.FILES)
    elif request.user.has_perm('trans.overwrite_translation'):
        form = UploadForm(request.POST, request.FILES)
    else:
        form = SimpleUploadForm(request.POST, request.FILES)

    # Check form validity
    if not form.is_valid():
        messages.error(request, _('Please fix errors in the form.'))
        return HttpResponseRedirect(obj.get_absolute_url())

    # Create author name
    if (request.user.has_perm('trans.author_translation')
            and form.cleaned_data['author_name'] != ''
            and form.cleaned_data['author_email'] != ''):
        author = '%s <%s>' % (
            form.cleaned_data['author_name'],
            form.cleaned_data['author_email']
        )
    else:
        author = None

    # Check for overwriting
    if request.user.has_perm('trans.overwrite_translation'):
        overwrite = form.cleaned_data['overwrite']
    else:
        overwrite = False

    # Do actual import
    try:
        ret = obj.merge_upload(
            request,
            request.FILES['file'],
            overwrite,
            author,
            merge_header=form.cleaned_data['merge_header'],
            method=form.cleaned_data['method']
        )
        if ret:
            messages.info(
                request,
                _('File content successfully merged into translation.')
            )
        else:
            messages.info(
                request,
                _('There were no new strings in uploaded file.')
            )
    except Exception as e:
        messages.error(
            request,
            _('File content merge failed: %s' % unicode(e))
        )

    return HttpResponseRedirect(obj.get_absolute_url())
