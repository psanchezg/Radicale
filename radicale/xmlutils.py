# -*- coding: utf-8 -*-
#
# This file is part of Radicale Server - Calendar Server
# Copyright © 2008-2011 Guillaume Ayoub
# Copyright © 2008 Nicolas Kandel
# Copyright © 2008 Pascal Halter
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Radicale.  If not, see <http://www.gnu.org/licenses/>.

"""
XML and iCal requests manager.

Note that all these functions need to receive unicode objects for full
iCal requests (PUT) and string objects with charset correctly defined
in them for XML requests (all but PUT).

"""

from datetime import datetime, timedelta, time, date
from dateutil.rrule import *
try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6 has no OrderedDict, use a dict instead
    OrderedDict = dict # pylint: disable=C0103
import re
import xml.etree.ElementTree as ET

from radicale import client, config, ical


NAMESPACES = {
    "C": "urn:ietf:params:xml:ns:caldav",
    "D": "DAV:",
    "CS": "http://calendarserver.org/ns/",
    "ICAL": "http://apple.com/ns/ical/",
    "ME": "http://me.com/_namespace/"}


NAMESPACES_REV = {}


for short, url in NAMESPACES.items():
    NAMESPACES_REV[url] = short
    if hasattr(ET, "register_namespace"):
        # Register namespaces cleanly with Python 2.7+ and 3.2+ ...
        ET.register_namespace("" if short == "D" else short, url)
    else:
        # ... and badly with Python 2.6 and 3.1
        ET._namespace_map[url] = short # pylint: disable=W0212


CLARK_TAG_REGEX = re.compile(r"""
    {                        # {
    (?P<namespace>[^}]*)     # namespace URL
    }                        # }
    (?P<tag>.*)              # short tag name
    """, re.VERBOSE)


def _pretty_xml(element, level=0):
    """Indent an ElementTree ``element`` and its children."""
    i = "\n" + level * "  "
    if len(element):
        if not element.text or not element.text.strip():
            element.text = i + "  "
        if not element.tail or not element.tail.strip():
            element.tail = i
        for sub_element in element:
            _pretty_xml(sub_element, level + 1)
        # ``sub_element`` is always defined as len(element) > 0
        # pylint: disable=W0631
        if not sub_element.tail or not sub_element.tail.strip():
            sub_element.tail = i
        # pylint: enable=W0631
    else:
        if level and (not element.tail or not element.tail.strip()):
            element.tail = i
    if not level:
        output_encoding = config.get("encoding", "request")
        return ('<?xml version="1.0"?>\n' + ET.tostring(
            element, "utf-8").decode("utf-8")).encode(output_encoding)


def _tag(short_name, local):
    """Get XML Clark notation {uri(``short_name``)}``local``."""
    return "{%s}%s" % (NAMESPACES[short_name], local)


def _tag_from_clark(name):
    """Get a human-readable variant of the XML Clark notation tag ``name``.

    For a given name using the XML Clark notation, return a human-readable
    variant of the tag name for known namespaces. Otherwise, return the name as
    is.

    """
    match = CLARK_TAG_REGEX.match(name)
    if match and match.group('namespace') in NAMESPACES_REV:
        args = {
            'ns': NAMESPACES_REV[match.group('namespace')],
            'tag': match.group('tag')}
        return '%(ns)s:%(tag)s' % args
    return name


def _response(code):
    """Return full W3C names from HTTP status codes."""
    return "HTTP/1.1 %i %s" % (code, client.responses[code])


def name_from_path(path, calendar):
    """Return Radicale item name from ``path``."""
    calendar_parts = calendar.local_path.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    return path_parts[-1] if (len(path_parts) - len(calendar_parts)) else None


def props_from_request(root, actions=("set", "remove")):
    """Return a list of properties as a dictionary."""
    result = OrderedDict()
    if not hasattr(root, "tag"):
        root = ET.fromstring(root.encode("utf8"))

    for action in actions:
        action_element = root.find(_tag("D", action))
        if action_element is not None:
            break
    else:
        action_element = root

    prop_element = action_element.find(_tag("D", "prop"))
    if prop_element is not None:
        for prop in prop_element:
            result[_tag_from_clark(prop.tag)] = prop.text

    return result


def delete(path, calendar):
    """Read and answer DELETE requests.

    Read rfc4918-9.6 for info.

    """
    # Reading request
    calendar.remove(name_from_path(path, calendar))

    # Writing answer
    multistatus = ET.Element(_tag("D", "multistatus"))
    response = ET.Element(_tag("D", "response"))
    multistatus.append(response)

    href = ET.Element(_tag("D", "href"))
    href.text = path
    response.append(href)

    status = ET.Element(_tag("D", "status"))
    status.text = _response(200)
    response.append(status)

    return _pretty_xml(multistatus)


def propfind(path, xml_request, calendars, user=None):
    """Read and answer PROPFIND requests.

    Read rfc4918-9.1 for info.

    """
    # Reading request
    root = ET.fromstring(xml_request.encode("utf8"))

    prop_element = root.find(_tag("D", "prop"))
    props = [prop.tag for prop in prop_element]

    # Writing answer
    multistatus = ET.Element(_tag("D", "multistatus"))

    for calendar in calendars:
        response = _propfind_response(path, calendar, props, user)
        multistatus.append(response)

    return _pretty_xml(multistatus)


def _propfind_response(path, item, props, user):
    """Build and return a PROPFIND response."""
    is_calendar = isinstance(item, ical.Calendar)
    if is_calendar:
        with item.props as cal_props:
            calendar_props = cal_props

    response = ET.Element(_tag("D", "response"))

    href = ET.Element(_tag("D", "href"))
    uri = item.url if is_calendar else "%s/%s" % (path, item.name)
    href.text = uri.replace("//", "/")
    response.append(href)

    propstat404 = ET.Element(_tag("D", "propstat"))
    propstat200 = ET.Element(_tag("D", "propstat"))
    response.append(propstat200)

    prop200 = ET.Element(_tag("D", "prop"))
    propstat200.append(prop200)

    prop404 = ET.Element(_tag("D", "prop"))
    propstat404.append(prop404)

    for tag in props:
        element = ET.Element(tag)
        is404 = False
        if tag == _tag("D", "getetag"):
            element.text = item.etag
        elif tag == _tag("D", "principal-URL"):
            tag = ET.Element(_tag("D", "href"))
            tag.text = path
            element.append(tag)
        elif tag in (
            _tag("D", "principal-collection-set"),
            _tag("C", "calendar-user-address-set"),
            _tag("C", "calendar-home-set")):
            tag = ET.Element(_tag("D", "href"))
            tag.text = path
            element.append(tag)
        elif tag == _tag("C", "supported-calendar-component-set"):
            # This is not a Todo
            # pylint: disable=W0511
            for component in ("VTODO", "VEVENT", "VJOURNAL"):
                comp = ET.Element(_tag("C", "comp"))
                comp.set("name", component)
                element.append(comp)
            # pylint: enable=W0511
        elif tag == _tag("D", "current-user-principal") and user:
            tag = ET.Element(_tag("D", "href"))
            tag.text = '/%s/' % user
            element.append(tag)
        elif tag == _tag("D", "current-user-privilege-set"):
            privilege = ET.Element(_tag("D", "privilege"))
            privilege.append(ET.Element(_tag("D", "all")))
            element.append(privilege)
        elif tag == _tag("D", "supported-report-set"):
            for report_name in (
                "principal-property-search", "sync-collection"
                "expand-property", "principal-search-property-set"):
                supported = ET.Element(_tag("D", "supported-report"))
                report_tag = ET.Element(_tag("D", "report"))
                report_tag.text = report_name
                supported.append(report_tag)
                element.append(supported)
        elif is_calendar:
            if tag == _tag("D", "getcontenttype"):
                element.text = "text/calendar"
            elif tag == _tag("D", "resourcetype"):
                if item.is_principal:
                    tag = ET.Element(_tag("D", "principal"))
                    element.append(tag)
                else:
                    tag = ET.Element(_tag("C", "calendar"))
                    element.append(tag)
                tag = ET.Element(_tag("D", "collection"))
                element.append(tag)
            elif tag == _tag("D", "owner") and item.owner_url:
                element.text = item.owner_url
            elif tag == _tag("CS", "getctag"):
                element.text = item.etag
            elif tag == _tag("C", "calendar-timezone"):
                element.text = ical.serialize(item.headers, item.timezones)
            else:
                human_tag = _tag_from_clark(tag)
                if human_tag in calendar_props:
                    element.text = calendar_props[human_tag]
                else:
                    is404 = True
        # Not for calendars
        elif tag == _tag("D", "getcontenttype"):
            element.text = "text/calendar; component=%s" % item.tag.lower()
        elif tag == _tag("D", "resourcetype"):
            # resourcetype must be returned empty for non-collection elements
            pass
        else:
            is404 = True

        if is404:
            prop404.append(element)
        else:
            prop200.append(element)

    status200 = ET.Element(_tag("D", "status"))
    status200.text = _response(200)
    propstat200.append(status200)

    status404 = ET.Element(_tag("D", "status"))
    status404.text = _response(404)
    propstat404.append(status404)
    if len(prop404):
        response.append(propstat404)

    return response


def _add_propstat_to(element, tag, status_number):
    """Add a PROPSTAT response structure to an element.

    The PROPSTAT answer structure is defined in rfc4918-9.1. It is added to the
    given ``element``, for the following ``tag`` with the given
    ``status_number``.

    """
    propstat = ET.Element(_tag("D", "propstat"))
    element.append(propstat)

    prop = ET.Element(_tag("D", "prop"))
    propstat.append(prop)

    if '{' in tag:
        clark_tag = tag
    else:
        clark_tag = _tag(*tag.split(':', 1))
    prop_tag = ET.Element(clark_tag)
    prop.append(prop_tag)

    status = ET.Element(_tag("D", "status"))
    status.text = _response(status_number)
    propstat.append(status)


def proppatch(path, xml_request, calendar):
    """Read and answer PROPPATCH requests.

    Read rfc4918-9.2 for info.

    """
    # Reading request
    root = ET.fromstring(xml_request.encode("utf8"))
    props_to_set = props_from_request(root, actions=('set',))
    props_to_remove = props_from_request(root, actions=('remove',))

    # Writing answer
    multistatus = ET.Element(_tag("D", "multistatus"))

    response = ET.Element(_tag("D", "response"))
    multistatus.append(response)

    href = ET.Element(_tag("D", "href"))
    href.text = path
    response.append(href)

    with calendar.props as calendar_props:
        for short_name, value in props_to_set.items():
            if short_name == 'C:calendar-timezone':
                calendar.replace('', value)
                calendar.write()
            else:
                calendar_props[short_name] = value
            _add_propstat_to(response, short_name, 200)
        for short_name in props_to_remove:
            try:
                del calendar_props[short_name]
            except KeyError:
                _add_propstat_to(response, short_name, 412)
            else:
                _add_propstat_to(response, short_name, 200)

    return _pretty_xml(multistatus)


def put(path, ical_request, calendar):
    """Read PUT requests."""
    name = name_from_path(path, calendar)
    if name in (item.name for item in calendar.items):
        # PUT is modifying an existing item
        calendar.replace(name, ical_request)
    else:
        # PUT is adding a new item
        calendar.append(name, ical_request)


def report(path, xml_request, calendar):
    """Read and answer REPORT requests.

    Read rfc3253-3.6 for info.

    """
    # Reading request
    root = ET.fromstring(xml_request.encode("utf8"))

    start = end = None
    expand = limit_recurrence_set = False
    prop_element = root.find(_tag("D", "prop"))
    props = []
    for prop in prop_element:
        props.append(prop.tag)
        for child in prop:
            if child.tag == _tag("C", "expand"):
                expand = True
            elif child.tag == _tag("C", "expand"):
                expand = limit_recurrence_set = True

    filter_element = root.find(_tag("C", "filter"))
    if filter_element is not None:
        for c in filter_element:
            for v in c:
                for filter_ in v:
                    if filter_.tag == _tag("C", "time-range"):
                        if 'start' in filter_.keys():
                            start = datetime.strptime(filter_.get('start'),
                                    '%Y%m%dT%H%M%SZ')
                        if 'end' in filter_.keys():
                            end = datetime.strptime(filter_.get('end'),
                                    '%Y%m%dT%H%M%SZ')

    if calendar:
        if root.tag == _tag("C", "calendar-multiget"):
            # Read rfc4791-7.9 for info
            hreferences = set(
                href_element.text for href_element
                in root.findall(_tag("D", "href")))
        else:
            hreferences = (path,)
    else:
        hreferences = ()

    # Writing answer
    multistatus = ET.Element(_tag("D", "multistatus"))

    for hreference in hreferences:
        # Check if the reference is an item or a calendar
        name = name_from_path(hreference, calendar)
        if name:
            # Reference is an item
            path = "/".join(hreference.split("/")[:-1]) + "/"
            items = (item for item in calendar.items if item.name == name)
        else:
            # Reference is a calendar
            path = hreference
            items = calendar.components

        new_items = []
        if expand:
            # Expand events
            for item in items:
                if item.rrule and end:
                    i = -1
                    for dtstart in rrulestr(item.rrule.rrule,
                            dtstart = item.dtstart):
                        if dtstart >= end:
                            break

                        if not start or dtstart > start:
                            i += 1
                            text = item.text
                            if i > 0:
                                text = re.sub(r"RRULE:.*\n",
                                        r"RECURRENCE-ID:%s\n" % \
                                        item.dtstart.strftime("%Y%m%dT%H%M%SZ"),
                                        text)
                                text = re.sub(r"SUMMARY:(.*)\n",
                                        r"SUMMARY:\1 (#%d)\n" % (i+1), text)

                            if not limit_recurrence_set:
                                # Remove rrule line
                                text = re.sub(r"RRULE:.*\n", "", text)

                            # Update start and end dates
                            dtend = dtstart + (item.dtend - item.dtstart)
                            text = re.sub(r"DTSTART:.*\n", "DTSTART:%s\n" % \
                                    dtstart.strftime("%Y%m%dT%H%M%SZ"), text)
                            text = re.sub(r"DTEND:.*\n", "DTEND:%s\n" % \
                                    dtend.strftime("%Y%m%dT%H%M%SZ"), text)
                            new_items.append(ical.Event(text, item.name))

                else:
                    if (not start or start < item.dtstart) and \
                            (not end or end > item.dtstart):
                        new_items.append(item)

        # Order events by start date
        if len(new_items) > 0:
            items = sorted(new_items, key=lambda item: item.dtstart)

        for item in items:
            response = ET.Element(_tag("D", "response"))
            multistatus.append(response)

            href = ET.Element(_tag("D", "href"))
            href.text = "%s/%s" % (path, item.name)
            response.append(href)

            propstat = ET.Element(_tag("D", "propstat"))
            response.append(propstat)

            prop = ET.Element(_tag("D", "prop"))
            propstat.append(prop)

            for tag in props:
                element = ET.Element(tag)
                if tag == _tag("D", "getetag"):
                    element.text = item.etag
                elif tag == _tag("C", "calendar-data"):
                    if isinstance(item, (ical.Event, ical.Todo, ical.Journal)):
                        element.text = ical.serialize(
                            calendar.headers, calendar.timezones + [item])
                prop.append(element)

            status = ET.Element(_tag("D", "status"))
            status.text = _response(200)
            propstat.append(status)

    return _pretty_xml(multistatus)
