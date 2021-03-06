# Core site concept heavily inspired by django.contrib.sites

from django.core.context_processors import csrf
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponse, HttpResponseNotModified, Http404
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.utils.datastructures import SortedDict
from django.utils.functional import update_wrapper
from django.utils.http import http_date
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.static import was_modified_since

from nexus import conf

import mimetypes
import os
import os.path
import posixpath
import stat
import urllib

NEXUS_ROOT = os.path.normpath(os.path.dirname(__file__))

class NexusSite(object):
    def __init__(self, name=None, app_name='nexus'):
        self._registry = {}
        self._categories = SortedDict()
        if name is None:
            self.name = 'nexus'
        else:
            self.name = name
        self.app_name = app_name

    def register_category(self, category, label, index=None):
        if index:
            self._categories.insert(index, category, label)
        else:
            self._categories[category] = label

    def register(self, module, namespace=None, category=None):
        module = module(self, category)
        if not namespace:
            namespace = module.get_namespace()
        if namespace:
            module.app_name = module.name = namespace
        self._registry[namespace] = (module, category)
        return module

    def get_urls(self):
        from django.conf.urls.defaults import patterns, url, include

        base_urls = patterns('',
            url(r'^media/(?P<module>[^/]+)/(?P<path>.+)$', self.media, name='nexus-media'),

            url(r'^$', self.as_view(self.dashboard), name='index'),
            url(r'^login/$', self.login, name='login'),
            url(r'^logout/$', self.as_view(self.logout), name='logout'),
        ), self.app_name, self.name
        
        urlpatterns = patterns('',
            url(r'^', include(base_urls)),
        )
        for namespace, module in self.get_modules():
            urlpatterns += patterns('',
                url(r'^%s/' % namespace, include(module.urls)),
            )
        
        return urlpatterns
    def urls(self):
        return self.get_urls()

    urls = property(urls)

    def has_permission(self, request):
        """
        Returns True if the given HttpRequest has permission to view
        *at least one* page in the admin site.
        """
        return request.user.is_active and request.user.is_staff

    def as_view(self, view, cacheable=False):
        "Wraps a view in authentication/caching logic"
        def inner(request, *args, **kwargs):
            if not self.has_permission(request):
                # show login pane
                return self.login(request, *args, **kwargs)
            return view(request, *args, **kwargs)

        # Mark it as never_cache
        if not cacheable:
            inner = never_cache(inner)

        # We add csrf_protect here so this function can be used as a utility
        # function for any view, without having to repeat 'csrf_protect'.
        if not getattr(view, 'csrf_exempt', False):
            inner = csrf_protect(inner)

        return update_wrapper(inner, view)

    def get_context(self, request):
        context = csrf(request)
        context.update({
            'request': request,
            'nexus_site': self,
            'nexus_media_prefix': conf.MEDIA_PREFIX.rstrip('/'),
        })
        return context

    def get_modules(self):
        for k, v in self._registry.iteritems():
            yield k, v[0]
    
    def get_module(self, module):
        return self._registry[module][0]

    def get_categories(self):
        for k, v in self._categories.iteritems():
            yield k, v

    def get_category_label(self, category):
        return self._categories.get(category, category.title().replace('_', ' '))

    def render_to_response(self, template, context, request, current_app=None):
        "Shortcut for rendering to response and default context instances"
        if not current_app:
            current_app = self.name
        else:
            current_app = '%s:%s' % (self.name, current_app)
        
        context_instance = RequestContext(request, current_app=current_app)

        context.update(self.get_context(request))
        
        return render_to_response(template, context,
            context_instance=context_instance
        )

    ## Our views
    
    def media(self, request, module, path):
        """
        Serve static files below a given point in the directory structure.
        """
        if module == 'nexus':
            document_root = os.path.join(NEXUS_ROOT, 'media')
        else:
            document_root = self.get_module(module).media_root
        
        path = posixpath.normpath(urllib.unquote(path))
        path = path.lstrip('/')
        newpath = ''
        for part in path.split('/'):
            if not part:
                # Strip empty path components.
                continue
            drive, part = os.path.splitdrive(part)
            head, part = os.path.split(part)
            if part in (os.curdir, os.pardir):
                # Strip '.' and '..' in path.
                continue
            newpath = os.path.join(newpath, part).replace('\\', '/')
        if newpath and path != newpath:
            return HttpResponseRedirect(newpath)
        fullpath = os.path.join(document_root, newpath)
        if os.path.isdir(fullpath):
            raise Http404("Directory indexes are not allowed here.")
        if not os.path.exists(fullpath):
            raise Http404('"%s" does not exist' % fullpath)
        # Respect the If-Modified-Since header.
        statobj = os.stat(fullpath)
        mimetype = mimetypes.guess_type(fullpath)[0] or 'application/octet-stream'
        if not was_modified_since(request.META.get('HTTP_IF_MODIFIED_SINCE'),
                                  statobj[stat.ST_MTIME], statobj[stat.ST_SIZE]):
            return HttpResponseNotModified(mimetype=mimetype)
        contents = open(fullpath, 'rb').read()
        response = HttpResponse(contents, mimetype=mimetype)
        response["Last-Modified"] = http_date(statobj[stat.ST_MTIME])
        response["Content-Length"] = len(contents)
        return response        

    def login(self, request):
        "Login form"
        from django.contrib.auth import login as login_
        from django.contrib.auth.forms import AuthenticationForm

        if request.POST:
            form = AuthenticationForm(request, request.POST)
            if form.is_valid():
                login_(request, form.get_user())
                return HttpResponseRedirect(request.POST.get('next') or reverse('nexus:index', current_app=self.name))
            else:
                request.session.set_test_cookie()
        else:
            form = AuthenticationForm(request)
            request.session.set_test_cookie()

        return self.render_to_response('nexus/login.html', {
            'form': form,
        }, request)
    login = never_cache(login)
    
    def logout(self, request):
        "Logs out user and redirects them to Nexus home"
        from django.contrib.auth import logout

        logout(request)

        return HttpResponseRedirect(reverse('nexus:index', current_app=self.name))

    def dashboard(self, request):
        "Basic dashboard panel"
        # TODO: these should be ajax
        module_set = []
        for namespace, module in self.get_modules():
            if module.home_url:
                home_url = reverse(module.get_home_url(), current_app=self.name)
            else:
                home_url = None

            if hasattr(module, 'render_on_dashboard'):
                module_set.append((module.get_dashboard_title(), module.render_on_dashboard(request), home_url))
        
        return self.render_to_response('nexus/dashboard.html', {
            'module_set': module_set,
        }, request)

# setup the default site

site = NexusSite()

