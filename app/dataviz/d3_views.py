import json
import math
import random

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.validators import validate_email
from django.db.models import Max
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from app.utils import sync_profile
from chartit import Chart, DataPool
from dashboard.models import Bounty, Profile, Tip, UserAction
from marketing.mails import new_feedback
from marketing.models import (
    EmailEvent, EmailSubscriber, GithubEvent, Keyword, LeaderboardRank, SlackPresence, SlackUser, Stat,
)
from marketing.utils import get_or_save_email_subscriber
from retail.helpers import get_ip


def data_viz_helper_get_data_responses(request, visual_type):
    """Handle visualization of the request response data based on type.

    Args:
        visual_type (str): The visualization type.

    TODO:
        * Reduce complexity of this method to pass McCabe complexity check.

    Returns:
        dict: The JSON representation of the requested visual type data.

    """
    data_dict = {}
    network = 'mainnet'
    for bounty in Bounty.objects.filter(network=network, web3_type='bounties_network', current_bounty=True):

        if visual_type == 'status_progression':
            max_size = 12
            value = 1
            if not value:
                continue
            response = []
            prev_bounties = Bounty.objects.filter(
                standard_bounties_id=bounty.standard_bounties_id,
                network=network
            ).exclude(pk=bounty.pk).order_by('created_on')
            if prev_bounties.exists() and prev_bounties.first().status == 'started':
                response.append('open')  # mock for status changes not mutating status
            last_bounty_status = None
            for prev_bounty in prev_bounties:
                if last_bounty_status != prev_bounty.status:
                    response.append(prev_bounty.status)
                last_bounty_status = prev_bounty.status
            if bounty.status != last_bounty_status:
                response.append(bounty.status)
            response = response[0:max_size]
            while len(response) < max_size:
                response.append('_')

        elif visual_type == 'repos':
            value = bounty.value_in_usdt_then

            response = [
                bounty.org_name.replace('-', ''),
                bounty.github_repo_name.replace('-', ''),
                str(bounty.github_issue_number),
            ]

        elif visual_type == 'fulfillers':
            response = []
            if bounty.status == 'done':
                for fulfillment in bounty.fulfillments.filter(accepted=1):
                    value = bounty.value_in_usdt_then

                    response = [
                        fulfillment.fulfiller_github_username.replace('-', '')
                    ]

        elif visual_type == 'funders':
            value = bounty.value_in_usdt_then
            response = []
            if bounty.bounty_owner_github_username and value:
                response = [
                    bounty.bounty_owner_github_username.replace('-', '')
                ]

        if response:
            response = '-'.join(response)
            if response in data_dict.keys():
                data_dict[response] += value
            else:
                data_dict[response] = value

    return data_dict


@staff_member_required
def viz_spiral(request, key='email_open'):
    """Render a spiral graph visualization.

    Args:
        key (str): The key type to visualize.

    Returns:
        TemplateResponse: The populated spiral data visualization template.

    """
    stats = Stat.objects.filter(created_on__hour=1)
    type_options = stats.distinct('key').values_list('key', flat=True)
    stats = stats.filter(key=key).order_by('created_on')
    params = {
        'stats': stats,
        'key': key,
        'page_route': 'spiral',
        'type_options': type_options,
        'viz_type': key,
    }
    return TemplateResponse(request, 'dataviz/spiral.html', params)


@staff_member_required
def viz_heatmap(request, key='email_open'):
    """Render a heatmap graph visualization.

    Args:
        key (str): The key type to visualize.

    Returns:
        JsonResponse: If data param provided, return a JSON representation of data to be graphed.
        TemplateResponse: If data param not provided, return the populated data visualization template.

    """
    time_now = timezone.now()
    stats = Stat.objects.filter(
        created_on__lt=time_now,
        created_on__gt=(time_now - timezone.timedelta(weeks=2))
    )
    type_options = stats.distinct('key').values_list('key', flat=True)
    stats = stats.filter(key=key).order_by('-created_on')

    if request.GET.get('data'):
        _max = max([stat.val_since_hour for stat in stats])
        output = {
            # {"timestamp": "2014-10-16T22:00:00", "value": {"PM2.5": 61.92}}
            "data": [{
                'timestamp': stat.created_on.strftime("%Y-%m-%dT%H:00:00"),
                'value': {"PM2.5": stat.val_since_hour * 800.0 / _max},
            } for stat in stats]
        }
        # Example output: https://gist.github.com/mbeacom/44f0114666d69bb5bf2756216c43b64d
        return JsonResponse(output)

    params = {
        'stats': stats,
        'key': key,
        'page_route': 'heatmap',
        'type_options': type_options,
        'viz_type': key,
    }
    return TemplateResponse(request, 'dataviz/heatmap.html', params)


@staff_member_required
def viz_index(request):
    """Render the visualization index.

    Returns:
        TemplateResponse: The visualization index template response.

    """
    return TemplateResponse(request, 'dataviz/index.html', {})


@staff_member_required
def viz_circles(request, visual_type):
    """Render a circle graph visualization.

    Args:
        visual_type (str): The visualization type.

    Returns:
        JsonResponse: If data param provided, return a JSON representation of data to be graphed.
        TemplateResponse: If data param not provided, return the populated data visualization template.

    """
    return viz_sunburst(request, visual_type, 'circles')


def data_viz_helper_merge_json_trees(output):
    """Handle merging the visualization data trees.

    Args:
        output (dict): The output data to be merged.

    Returns:
        dict: The merged data dictionary.

    """
    new_output = {
        'name': output['name'],
    }
    if not output.get('children'):
        new_output['size'] = output['size']
        return new_output

    # merge in names that are equal
    new_output['children'] = []
    processed_names = {}
    length = len(output['children'])
    for i in range(0, length):
        this_child = output['children'][i]
        name = this_child['name']
        if name in processed_names.keys():
            target_idx = processed_names[name]
            print(target_idx)
            for this_childs_child in this_child['children']:
                new_output['children'][target_idx]['children'].append(this_childs_child)
        else:
            processed_names[name] = len(new_output['children'])
            new_output['children'].append(this_child)

    # merge further down the line
    length = len(new_output['children'])
    for i in range(0, length):
        new_output['children'][i] = data_viz_helper_merge_json_trees(new_output['children'][i])

    return new_output


def data_viz_helper_get_json_output(key, value, depth=0):
    """Handle data visualization and build the JSON output.

    Args:
        key (str): The key to be formatted and parsed.
        value (float): The data value.
        depth (int): The depth of keys to parse. Defaults to: 0.

    Returns:
        dict: The JSON representation of the provided data.

    """
    keys = key.replace('_', '').split('-')
    result = {'name': keys[0]}
    if len(keys) > 1:
        result['children'] = [
            data_viz_helper_get_json_output("-".join(keys[1:]), value, depth + 1)
        ]
    else:
        result['size'] = int(value)
    return result


@staff_member_required
def viz_sunburst(request, visual_type, template='sunburst'):
    """Render a sunburst graph visualization.

    Args:
        visual_type (str): The visualization type.
        template (str): The template type to be used. Defaults to: sunburst.

    TODO:
        * Reduce the number of local variables in this method from 18 to 15.

    Returns:
        JsonResponse: If data param provided, return a JSON representation of data to be graphed.
        TemplateResponse: If data param not provided, return the populated data visualization template.

    """
    visual_type_options = [
        'status_progression',
        'repos',
        'fulfillers',
        'funders',
    ]
    if visual_type not in visual_type_options:
        visual_type = visual_type_options[0]

    if visual_type == 'status_progression':
        title = "Status Progression Viz"
        comment = 'of statuses begin with this sequence of status'
        categories = list(Bounty.objects.distinct('idx_status').values_list('idx_status', flat=True)) + ['_']
    elif visual_type == 'repos':
        title = "Github Structure of All Bounties"
        comment = 'of bounties value with this github structure'
        categories = [bounty.org_name.replace('-', '') for bounty in Bounty.objects.filter(network='mainnet')]
        categories += [bounty.github_repo_name.replace('-', '') for bounty in Bounty.objects.filter(network='mainnet')]
        categories += [str(bounty.github_issue_number) for bounty in Bounty.objects.filter(network='mainnet')]
    elif visual_type == 'fulfillers':
        title = "Fulfillers"
        comment = 'of bounties value with this fulfiller'
        categories = []
        for bounty in Bounty.objects.filter(network='mainnet'):
            for fulfiller in bounty.fulfillments.all():
                categories.append(fulfiller.fulfiller_github_username.replace('-', ''))
    elif visual_type == 'funders':
        title = "Funders"
        comment = 'of bounties value with this funder'
        categories = []
        for bounty in Bounty.objects.filter(network='mainnet'):
            categories.append(bounty.bounty_owner_github_username.replace('-', ''))

    if request.GET.get('data'):
        data_dict = data_viz_helper_get_data_responses(request, visual_type)

        _format = request.GET.get('format', 'csv')
        if _format == 'csv':
            rows = []
            for key, value in data_dict.items():
                row = ",".join([key, str(value)])
                rows.append(row)

            output = "\n".join(rows)
            return HttpResponse(output)

        if _format == 'json':
            output = {
                'name': 'data',
                'children': [
                ]
            }
            for key, val in data_dict.items():
                if val:
                    output['children'].append(data_viz_helper_get_json_output(key, val))
            output = data_viz_helper_merge_json_trees(output)
            return JsonResponse(output)

    params = {
        'title': title,
        'comment': comment,
        'viz_type': visual_type,
        'page_route': template,
        'type_options': visual_type_options,
        'categories': json.dumps(list(categories)),
    }
    return TemplateResponse(request, f'dataviz/{template}.html', params)


@staff_member_required
def viz_graph(request, _type):
    """Render a graph visualization of the Gitcoin Network.

    TODO:
        * Reduce the number of local variables from 16 to 15.

    Returns:
        JsonResponse: If data param provided, return a JSON representation of data to be graphed.
        TemplateResponse: If data param not provided, return the populated data visualization template.

    """
    _type_options = ['fulfillments_accepted_only', 'all', 'fulfillments', 'what_future_could_look_like']
    if _type not in _type_options:
        _type = _type_options[0]
    title = 'Graph of Gitcoin Network'
    if request.GET.get('data'):
        # setup response
        output = {
            "nodes": [],
            "links": []
        }

        # gather info
        types = {}
        names = {}
        values = {}
        avatars = {}
        edges = []
        for bounty in Bounty.objects.filter(network='mainnet', current_bounty=True):
            if bounty.value_in_usdt_then:
                weight = bounty.value_in_usdt_then
                source = bounty.org_name
                if source:
                    for fulfillment in bounty.fulfillments.all():
                        if _type != 'fulfillments_accepted_only' or fulfillment.accepted:
                            target = fulfillment.fulfiller_github_username.lower()
                            types[source] = 'source'
                            types[target] = 'target_accepted' if fulfillment.accepted else 'target'
                            names[source] = None
                            names[target] = None
                            edges.append((source, target, weight))

                            value = values.get(source, 0)
                            value += weight
                            values[source] = value
                            value = values.get(target, 0)
                            value += weight
                            values[target] = value

        for tip in Tip.objects.filter(network='mainnet'):
            weight = bounty.value_in_usdt
            if weight:
                source = tip.username.lower()
                target = tip.from_username.lower()
                if source and target:
                    if source not in names.keys():
                        types[source] = 'source'
                        names[source] = None
                    if source not in types.keys():
                        types[target] = 'target'
                        names[target] = None
                    edges.append((source, target, weight))


        if _type in ['what_future_could_look_like', 'all']:
            last_node = None
            nodes = Profile.objects.exclude(github_access_token='').all()
            for profile in nodes:
                node = profile.handle.lower()
                if node not in names.keys():
                    names[node] = None
                    types[node] = 'independent'
                if last_node and _type == 'what_future_could_look_like': # and random.randint(0, 2) == 0:
                        weight = random.randint(1, 10)
                        #edges.append((node, last_node, weight))
                        #edges.append((nodes.order_by('?').first().handle.lower(), node, weight))
                        edges.append((nodes.order_by('?').first().handle.lower(), node, weight))
                last_node = node


        for key, val in values.items():
            if val > 40:
                github_url = f"https://github.com/{key}"
                avatars[key] = f'https://gitcoin.co/funding/avatar?repo={github_url}&v=3'

        # build output
        for name in set(names.keys()):
            names[name] = len(output['nodes'])
            value = int(math.sqrt(math.sqrt(values.get(name, 1))))
            output['nodes'].append({"name": name, 'value': value, 'type': types.get(name), 'avatar': avatars.get(name)})
        for edge in edges:
            source, target, weight = edge
            weight = math.sqrt(weight)
            source = names[source]
            target = names[target]
            output['links'].append({
                'source': source,
                'target': target,
                'weight': weight,
            })

        return JsonResponse(output)

    params = {
        'title': title,
        'viz_type': _type,
        'type_options': _type_options,
        'page_route': 'graph',
    }
    return TemplateResponse(request, f'dataviz/graph.html', params)