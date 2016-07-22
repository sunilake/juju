#!/usr/bin/env python
"""Simple performance and scale tests."""

from __future__ import print_function

import argparse
from collections import defaultdict, namedtuple
from contextlib import contextmanager
from datetime import datetime
from jinja2 import Template
import logging
import os
import sys
import subprocess
import time
from textwrap import dedent

from fixtures import EnvironmentVariable

from deploy_stack import (
    BootstrapManager,
)
from logbreakdown import breakdown_log_by_timeframes
from utility import (
    add_basic_testing_arguments,
    configure_logging,
    temp_dir,
)


__metaclass__ = type


class TimingData:

    strf_format = '%F %H:%M:%S'

    def __init__(self, start, end):
        self._start = start
        self._end = end
        self._seconds = int((end - start).total_seconds())

    @property
    def start(self):
        return self._start.strftime(self.strf_format)

    @property
    def end(self):
        return self._end.strftime(self.strf_format)

    @property
    def seconds(self):
        return self._seconds

DeployDetails = namedtuple(
    'DeployDetails', ['name', 'applications', 'timings'])


collectd_config = dedent("""\
# Global

Hostname "localhost"

# Interval at which to query values. This may be overwritten on a per-plugin #
Interval 3

# Logging

LoadPlugin logfile
# LoadPlugin syslog

<Plugin logfile>
    LogLevel "debug"
    File STDOUT
    Timestamp true
    PrintSeverity false
</Plugin>

# LoadPlugin section

LoadPlugin aggregation
LoadPlugin cpu
#LoadPlugin cpufreq
LoadPlugin csv
LoadPlugin df
LoadPlugin disk
LoadPlugin entropy
LoadPlugin interface
LoadPlugin irq
LoadPlugin load
LoadPlugin memory
LoadPlugin processes
LoadPlugin rrdtool
LoadPlugin swap
LoadPlugin users

# Plugin configuration                                                       #

<Plugin "aggregation">
    <Aggregation>
        #Host "unspecified"
        Plugin "cpu"
        # PluginInstance "/[0,2,4,6,8]$/"
        Type "cpu"
        #TypeInstance "unspecified"

        # SetPlugin "cpu"
        # SetPluginInstance "even-%{aggregation}"

        GroupBy "Host"
        GroupBy "TypeInstance"

        CalculateNum true
        CalculateSum true
        CalculateAverage true
        CalculateMinimum false
        CalculateMaximum false
        CalculateStddev false
    </Aggregation>
</Plugin>

<Plugin csv>
    DataDir "/var/lib/collectd/csv"
    StoreRates true
</Plugin>

<Plugin df>
#    Device "/dev/sda1"
#    Device "192.168.0.2:/mnt/nfs"
#    MountPoint "/home"
#    FSType "ext3"

    # ignore rootfs; else, the root file-system would appear twice, causing
    # one of the updates to fail and spam the log
    FSType rootfs
    # ignore the usual virtual / temporary file-systems
    FSType sysfs
    FSType proc
    FSType devtmpfs
    FSType devpts
    FSType tmpfs
    FSType fusectl
    FSType cgroup
    IgnoreSelected true

#    ReportByDevice false
#    ReportReserved false
#    ReportInodes false

#    ValuesAbsolute true
#    ValuesPercentage false
</Plugin>

<Plugin rrdtool>
    DataDir "/var/lib/collectd/rrd"
#    CacheTimeout 120
#    CacheFlush 900
#    WritesPerSecond 30
#    CreateFilesAsync false
#    RandomTimeout 0
#
# The following settings are rather advanced
# and should usually not be touched:
#    StepSize 10
#    HeartBeat 20
#    RRARows 1200
#    RRATimespan 158112000
#    XFF 0.1
</Plugin>

<Include "/etc/collectd/collectd.conf.d">
    Filter "*.conf"
</Include>
""")


log = logging.getLogger("assess_perf_test_simple")


def assess_perf_test_simple(bs_manager, upload_tools):
    # XXX
    # Find the actual cause for this!! (Something to do with the template and
    # the logs.)
    import sys
    reload(sys)  # NOQA
    sys.setdefaultencoding('utf-8')
    # XXX

    bs_start = datetime.utcnow()
    # Make sure you check the responsiveness of 'juju status'
    with bs_manager.booted_context(upload_tools):
        try:
            client = bs_manager.client
            admin_client = client.get_admin_client()
            admin_client.wait_for_started()
            bs_end = datetime.utcnow()
            bootstrap_timing = TimingData(bs_start, bs_end)

            setup_system_monitoring(admin_client)

            deploy_details = assess_deployment_perf(client)
        finally:
            results_dir = os.path.join(
                os.path.abspath(bs_manager.log_dir), 'performance_results/')
            os.makedirs(results_dir)
            admin_client.juju(
                'scp',
                ('--', '-r', '0:/var/lib/collectd/rrd/localhost/*',
                 results_dir)
            )

            admin_client.juju(
                'scp', ('0:/tmp/mongodb-stats.log', results_dir)
            )
            cleanup_start = datetime.utcnow()
    # Cleanup happens when we move out of context
    cleanup_end = datetime.utcnow()
    cleanup_timing = TimingData(cleanup_start, cleanup_end)
    deployments = dict(
        bootstrap=bootstrap_timing,
        deploys=[deploy_details],
        cleanup=cleanup_timing,
    )

    # Could be smarter about this.
    controller_log_file = os.path.join(
        bs_manager.log_dir,
        'controller',
        'machine-0',
        'machine-0.log.gz')

    generate_reports(controller_log_file, results_dir, deployments)


def generate_reports(controller_log, results_dir, deployments):
    """Generate reports and graphs from run results."""
    cpu_image = generate_cpu_graph_image(results_dir)
    memory_image = generate_memory_graph_image(results_dir)
    swap_image = generate_swap_graph_image(results_dir)
    network_image = generate_network_graph_image(results_dir)
    mongo_image = generate_mongo_graph_image(results_dir)

    log_message_chunks = get_log_message_in_timed_chunks(
        controller_log, deployments)

    details = dict(
        cpu_graph=cpu_image,
        memory_graph=memory_image,
        swap_graph=swap_image,
        network_graph=network_image,
        mongo_graph=mongo_image,
        deployments=deployments,
        log_message_chunks=log_message_chunks
    )

    create_html_report(results_dir, details)


def get_log_message_in_timed_chunks(log_file, deployments):
    # get timeframesfor the different actions.
    # Hmm, these are actually stored as datetime objects, no need for the
    # string conversions etc.
    bootstrap_timings = (
        deployments['bootstrap'].start, deployments['bootstrap'].end)

    cleanup_timings = (
        deployments['cleanup'].start, deployments['cleanup'].end)

    deploy_timings = [
        (d.timings.start, d.timings.end)
        for d in deployments['deploys']]

    # name_lookup = {
    #     '2016-07-21 08:33:12 - 2016-07-21 08:38:15': 'Bootstrap',
    #     '2016-07-21 08:39:46 - 2016-07-21 08:42:42': 'Delpoy',
    #     '2016-07-21 08:42:43 - 2016-07-21 08:43:14': 'Kill Controller',
    # }

    def _render_ds_string(start, end):
        return '{} - {}'.format(start, end)

    bs_name = _render_ds_string(
        deployments['bootstrap'].start,
        deployments['bootstrap'].end)

    cleanup_name = _render_ds_string(
        deployments['cleanup'].start,
        deployments['cleanup'].end)

    name_lookup = {
        bs_name: 'Bootstrap',
        cleanup_name: 'Kill-Controller',
    }
    for dep in deployments['deploys']:
        name_range = _render_ds_string(
            dep.timings.start, dep.timings.end)
        name_lookup[name_range] = dep.name

    all_timings = [bootstrap_timings] + deploy_timings + [cleanup_timings]

    raw_details = breakdown_log_by_timeframes(log_file, all_timings)

    event_details = defaultdict(defaultdict)
    # Outer-layer (i.e. event)
    for event_range in raw_details.keys():
        event_details[event_range]['name'] = name_lookup[event_range]
        event_details[event_range]['logs'] = []

        for log_range in raw_details[event_range].keys():
            timeframe = log_range
            message = '<br/>'.join(raw_details[event_range][log_range])
            event_details[event_range]['logs'].append(
                dict(
                    timeframe=timeframe,
                    message=message))

    return event_details


def create_html_report(results_dir, details):
    # render the html file to the results dir
    with open('./perf_report_template.html', 'rt') as f:
        template = Template(f.read())

    results_output = os.path.join(results_dir, 'report.html')
    with open(results_output, 'wt') as f:
        f.write(template.render(details))


def generate_graph_image(base_dir, results_dir, generator):
    metric_files_dir = os.path.join(os.path.abspath(base_dir), results_dir)
    return generator(metric_files_dir, base_dir)


def create_report_graph(rrd_dir, output_dir, name, generator):
    any_file = os.listdir(rrd_dir)[0]
    start, end = get_duration_points(os.path.join(rrd_dir, any_file))
    output_file = os.path.join(
        os.path.abspath(output_dir), '{}.png'.format(name))
    generator(start, end, rrd_dir, output_file)
    print('Created: {}'.format(output_file))
    return output_file


def generate_cpu_graph_image(results_dir):
    return generate_graph_image(
        results_dir, 'aggregation-cpu-average', create_cpu_report_graph)


def create_cpu_report_graph(rrd_dir, output_dir):
    return create_report_graph(rrd_dir, output_dir, 'cpu', _rrd_cpu_graph)


def generate_memory_graph_image(results_dir):
    return generate_graph_image(
        results_dir, 'memory', create_memory_report_graph)


def create_memory_report_graph(rrd_dir, output_dir):
    return create_report_graph(
        rrd_dir, output_dir, 'memory', _rrd_memory_graph)


def generate_swap_graph_image(results_dir):
    return generate_graph_image(results_dir, 'swap', create_swap_report_graph)


def create_swap_report_graph(rrd_dir, output_dir):
    return create_report_graph(rrd_dir, output_dir, 'swap', _rrd_swap_graph)


def generate_network_graph_image(results_dir):
    return generate_graph_image(
        results_dir, 'interface-eth0', create_network_report_graph)


def create_network_report_graph(rrd_dir, output_dir):
    return create_report_graph(
        rrd_dir, output_dir, 'network', _rrd_network_graph)


def generate_mongo_graph_image(results_dir):
    return generate_graph_image(
        results_dir, 'aggregation-cpu-average', create_mongodb_report_graph)


def create_mongodb_report_graph(rrd_dir, output_dir):
    return create_report_graph(
        rrd_dir, output_dir, 'mongodb', _rrd_mongdb_graph)


def _rrd_network_graph(start, end, rrd_path, output_file):
    script_text = dedent("""\
    rrdtool graph {output_file} -a PNG --full-size-mode \
    -s '{start}' -e '{end}' -w '800' -h '600' \
    '-n' \
    'DEFAULT:0:Bitstream Vera Sans' \
    '-v' \
    'Memory' \
    '-r' \
    '-t' \
    'Memory Usage' \
    'DEF:rx_avg={rrd_path}/if_packets.rrd:rx:AVERAGE' \
    'DEF:tx_avg={rrd_path}/if_packets.rrd:tx:AVERAGE' \
    'CDEF:rx_nnl=rx_avg,UN,0,rx_avg,IF' \
    'CDEF:tx_nnl=tx_avg,UN,0,tx_avg,IF' \
    'CDEF:rx_stk=rx_nnl' \
    'CDEF:tx_stk=tx_nnl' \
    'LINE2:rx_stk#bff7bf: rx' \
    'LINE2:tx_stk#ffb000: tx'
    """.format(
        output_file=output_file,
        rrd_path=rrd_path,
        start=start,
        end=end))
    with EnvironmentVariable('TZ', 'UTC'):
        with temp_dir() as temp:
            script_path = os.path.abspath(os.path.join(temp, 'network.sh'))
            with open(script_path, 'wt') as f:
                f.write(script_text)
            subprocess.check_call(['sh', script_path])


def _rrd_swap_graph(start, end, rrd_path, output_file):
    script_text = dedent("""\
    rrdtool graph "{output_file}" -a PNG --full-size-mode \
    -s "{start}" -e "{end}" -w "800" -h "600" \
    "-n" \
    "DEFAULT:0:Bitstream Vera Sans" \
    "-v" \
    "Swap" \
    "-r" \
    "-t" \
    "Swap space Usage" \
    "DEF:free_avg={rrd_path}/swap-free.rrd:value:AVERAGE" \
    "CDEF:free_nnl=free_avg,UN,0,free_avg,IF" \
    "DEF:used_avg={rrd_path}/swap-used.rrd:value:AVERAGE" \
    "CDEF:used_nnl=used_avg,UN,0,used_avg,IF" \
    "CDEF:free_stk=free_nnl" \
    "CDEF:used_stk=used_nnl" \
    "AREA:free_stk#ffffff" \
    "LINE1:free_stk#ffffff:free" \
    "AREA:used_stk#ffebbf" \
    "LINE1:used_stk#ffb000:used"
    """.format(
        output_file=output_file,
        rrd_path=rrd_path,
        start=start,
        end=end))
    with EnvironmentVariable('TZ', 'UTC'):
        with temp_dir() as temp:
            script_path = os.path.abspath(os.path.join(temp, 'swap.sh'))
            with open(script_path, 'wt') as f:
                f.write(script_text)
            subprocess.check_call(['sh', script_path])


def _rrd_memory_graph(start, end, rrd_path, output_file):
    script_text = dedent("""\
    rrdtool graph "{output_file}" -a PNG --full-size-mode \
    -s "{start}" -e "{end}" -w "800" -h "600" \
    "-n" \
    "DEFAULT:0:Bitstream Vera Sans" \
    "-v" \
    "Memory" \
    "-r" \
    "-t" \
    "Memory Usage" \
    "DEF:free_avg={rrd_path}/memory-free.rrd:value:AVERAGE" \
    "CDEF:free_nnl=free_avg,UN,0,free_avg,IF" \
    "DEF:used_avg={rrd_path}/memory-used.rrd:value:AVERAGE" \
    "CDEF:used_nnl=used_avg,UN,0,used_avg,IF" \
    "DEF:buffered_avg={rrd_path}/memory-buffered.rrd:value:AVERAGE" \
    "CDEF:buffered_nnl=buffered_avg,UN,0,buffered_avg,IF" \
    "DEF:cached_avg={rrd_path}/memory-cached.rrd:value:AVERAGE" \
    "CDEF:cached_nnl=cached_avg,UN,0,cached_avg,IF" \
    "CDEF:free_stk=free_nnl" \
    "CDEF:used_stk=used_nnl" \
    "AREA:free_stk#ffffff" \
    "LINE1:free_stk#ffffff:free" \
    "AREA:used_stk#ffebbf" \
    "LINE1:used_stk#ffb000:used"
    """.format(
        output_file=output_file,
        rrd_path=rrd_path,
        start=start,
        end=end))
    with EnvironmentVariable('TZ', 'UTC'):
        with temp_dir() as temp:
            script_path = os.path.abspath(os.path.join(temp, 'memory.sh'))
            with open(script_path, 'wt') as f:
                f.write(script_text)
            subprocess.check_call(['sh', script_path])


def _rrd_mongdb_graph(start, end, rrd_path, output_file):
    script_text = dedent("""\
    rrdtool graph {output_file} -a PNG --full-size-mode \
    -s '{start}' -e '{end}' -w '800' -h '600' \
    '-n' \
    'DEFAULT:0:Bitstream Vera Sans' \
    '-v' \
    'Count' \
    '-r' \
    '-u' \
    '100' \
    '-t' \
    'Mongo Statistics' \
    'DEF:idle_avg={rrd_path}/cpu-idle.rrd:value:AVERAGE' \
    'CDEF:idle_nnl=idle_avg,UN,0,idle_avg,IF' \
    'DEF:wait_avg={rrd_path}/cpu-wait.rrd:value:AVERAGE' \
    'CDEF:wait_nnl=wait_avg,UN,0,wait_avg,IF' \
    'DEF:nice_avg={rrd_path}/cpu-nice.rrd:value:AVERAGE' \
    'CDEF:nice_nnl=nice_avg,UN,0,nice_avg,IF' \
    'DEF:user_avg={rrd_path}/cpu-user.rrd:value:AVERAGE' \
    'CDEF:user_nnl=user_avg,UN,0,user_avg,IF' \
    'DEF:system_avg={rrd_path}/cpu-system.rrd:value:AVERAGE' \
    'CDEF:system_nnl=system_avg,UN,0,system_avg,IF' \
    'DEF:softirq_avg={rrd_path}/cpu-softirq.rrd:value:AVERAGE' \
    'CDEF:softirq_nnl=softirq_avg,UN,0,softirq_avg,IF' \
    'DEF:interrupt_avg={rrd_path}/cpu-interrupt.rrd:value:AVERAGE' \
    'CDEF:interrupt_nnl=interrupt_avg,UN,0,interrupt_avg,IF' \
    'DEF:steal_avg={rrd_path}/cpu-steal.rrd:value:AVERAGE' \
    'CDEF:steal_nnl=steal_avg,UN,0,steal_avg,IF' \
    'CDEF:steal_stk=steal_nnl' \
    'CDEF:interrupt_stk=interrupt_nnl,steal_stk,+' \
    'CDEF:softirq_stk=softirq_nnl,interrupt_stk,+' \
    'CDEF:system_stk=system_nnl,softirq_stk,+' \
    'CDEF:user_stk=user_nnl,system_stk,+' \
    'CDEF:nice_stk=nice_nnl,user_stk,+' \
    'CDEF:wait_stk=wait_nnl,nice_stk,+' \
    'CDEF:idle_stk=idle_nnl,wait_stk,+' \
    'AREA:idle_stk#ffffff' \
    'LINE1:idle_stk#ffffff: insert' \
    'AREA:wait_stk#ffebbf' \
    'LINE1:wait_stk#ffb000: query' \
    'AREA:nice_stk#bff7bf' \
    'LINE1:nice_stk#00e000: update' \
    'AREA:user_stk#bfbfff' \
    'LINE1:user_stk#0000ff: delete'
    """.format(
        output_file=output_file,
        rrd_path=rrd_path,
        start=start,
        end=end
    ))

    with EnvironmentVariable('TZ', 'UTC'):
        with temp_dir() as temp:
            script_path = os.path.abspath(os.path.join(temp, 'mongo.sh'))
            with open(script_path, 'wt') as f:
                f.write(script_text)
            subprocess.check_call(['sh', script_path])


def _rrd_cpu_graph(start, end, rrd_path, output_file):
    script_text = dedent("""\
    rrdtool graph {output_file} -a PNG --full-size-mode \
    -s '{start}' -e '{end}' -w '800' -h '600' \
    '-n' \
    'DEFAULT:0:Bitstream Vera Sans' \
    '-v' \
    'Jiffies' \
    '-r' \
    '-u' \
    '100' \
    '-t' \
    'CPU Average' \
    'DEF:idle_avg={rrd_path}/cpu-idle.rrd:value:AVERAGE' \
    'CDEF:idle_nnl=idle_avg,UN,0,idle_avg,IF' \
    'DEF:wait_avg={rrd_path}/cpu-wait.rrd:value:AVERAGE' \
    'CDEF:wait_nnl=wait_avg,UN,0,wait_avg,IF' \
    'DEF:nice_avg={rrd_path}/cpu-nice.rrd:value:AVERAGE' \
    'CDEF:nice_nnl=nice_avg,UN,0,nice_avg,IF' \
    'DEF:user_avg={rrd_path}/cpu-user.rrd:value:AVERAGE' \
    'CDEF:user_nnl=user_avg,UN,0,user_avg,IF' \
    'DEF:system_avg={rrd_path}/cpu-system.rrd:value:AVERAGE' \
    'CDEF:system_nnl=system_avg,UN,0,system_avg,IF' \
    'DEF:softirq_avg={rrd_path}/cpu-softirq.rrd:value:AVERAGE' \
    'CDEF:softirq_nnl=softirq_avg,UN,0,softirq_avg,IF' \
    'DEF:interrupt_avg={rrd_path}/cpu-interrupt.rrd:value:AVERAGE' \
    'CDEF:interrupt_nnl=interrupt_avg,UN,0,interrupt_avg,IF' \
    'DEF:steal_avg={rrd_path}/cpu-steal.rrd:value:AVERAGE' \
    'CDEF:steal_nnl=steal_avg,UN,0,steal_avg,IF' \
    'CDEF:steal_stk=steal_nnl' \
    'CDEF:interrupt_stk=interrupt_nnl,steal_stk,+' \
    'CDEF:softirq_stk=softirq_nnl,interrupt_stk,+' \
    'CDEF:system_stk=system_nnl,softirq_stk,+' \
    'CDEF:user_stk=user_nnl,system_stk,+' \
    'CDEF:nice_stk=nice_nnl,user_stk,+' \
    'CDEF:wait_stk=wait_nnl,nice_stk,+' \
    'CDEF:idle_stk=idle_nnl,wait_stk,+' \
    'AREA:idle_stk#ffffff' \
    'LINE1:idle_stk#ffffff:idle     ' \
    'AREA:wait_stk#ffebbf' \
    'LINE1:wait_stk#ffb000:wait     ' \
    'AREA:nice_stk#bff7bf' \
    'LINE1:nice_stk#00e000:nice     ' \
    'AREA:user_stk#bfbfff' \
    'LINE1:user_stk#0000ff:user     ' \
    'AREA:system_stk#ffbfbf' \
    'LINE1:system_stk#ff0000:system   ' \
    'AREA:softirq_stk#ffbfff' \
    'LINE1:softirq_stk#ff00ff:softirq  ' \
    'AREA:interrupt_stk#e7bfe7' \
    'LINE1:interrupt_stk#a000a0:interrupt' \
    'AREA:steal_stk#bfbfbf' \
    'LINE1:steal_stk#000000:steal'
    """.format(
        output_file=output_file,
        rrd_path=rrd_path,
        start=start,
        end=end
    ))

    with EnvironmentVariable('TZ', 'UTC'):
        with temp_dir() as temp:
            script_path = os.path.abspath(os.path.join(temp, 'cpu.sh'))
            with open(script_path, 'wt') as f:
                f.write(script_text)
            subprocess.check_call(['sh', script_path])


def get_duration_points(rrd_file):
    start = subprocess.check_output(['rrdtool', 'first', rrd_file]).strip()
    end = subprocess.check_output(['rrdtool', 'last', rrd_file]).strip()

    # This probably stems from a misunderstanding on my part.
    shitty_command = 'rrdtool fetch {file} AVERAGE --start {start} --end {end} | tail --lines=+3 | grep -v "\-nan" | head -n1'.format(
        file=rrd_file,
        start=start,
        end=end)
    actual_start = subprocess.check_output(shitty_command, shell=True)

    return actual_start.split(':')[0], end


def assess_deployment_perf(client):
    # This is where multiple services are started either at the same time
    # or one after the other etc.
    deploy_start = datetime.utcnow()

    # bundle_name = 'cs:bundle/wiki-simple-0'
    bundle_name = 'cs:ubuntu'
    client.deploy(bundle_name)
    client.wait_for_started()

    deploy_end = datetime.utcnow()
    deploy_timing = TimingData(deploy_start, deploy_end)

    # get details like unit count etc.
    client_details = get_client_details(client)

    # ensure the service is responding.
    # Collect data results.
    return DeployDetails(bundle_name, client_details, deploy_timing)


def get_client_details(client):
    status = client.get_status()
    units = dict()
    for name in status.get_applications().keys():
        units[name] = status.get_service_unit_count(name)
    return units


def setup_system_monitoring(admin_client):
    # Using ssh get into the machine-0 (or all api/state servers)
    # Install the required packages and start up logging.
    admin_client.juju('ssh', ('0', 'sudo apt-get install -y collectd-core'))
    admin_client.juju('ssh', ('0', 'sudo mkdir /etc/collectd/collectd.conf.d'))
    with temp_dir() as temp:
        path = os.path.join(temp, 'collectd.conf')
        with open(path, 'w') as f:
            f.write(collectd_config)
        admin_client.juju('scp', (path, '0:/tmp/collectd.config'))
    admin_client.juju(
        'ssh',
        ('0', 'sudo cp /tmp/collectd.config /etc/collectd/collectd.conf'))
    admin_client.juju('ssh', ('0', 'sudo /etc/init.d/collectd restart'))

    script_contents = dedent("""\
    password=`sudo grep oldpassword \
      /var/lib/juju/agents/machine-*/agent.conf  | cut -d" " -f2`
    mongostat --host=127.0.0.1:37017 --authenticationDatabase admin --ssl \
      --sslAllowInvalidCertificates --username \"admin\" \
      --password \"\$password\" --noheaders 5 > /tmp/mongodb-stats.log
    """)
    admin_client.juju(
        'ssh',
        ('0', '--', 'echo "{}" > /tmp/runner.sh'.format(script_contents)))

    commands = [
        'sudo echo "deb http://repo.mongodb.org/apt/ubuntu xenial/mongodb-org/testing multiverse" | sudo tee /etc/apt/sources.list.d/mongo-org.list',
        'sudo apt-get update',
        'sudo apt-get install --yes --allow-unauthenticated mongodb-org-tools daemon',
        'sudo chmod +x /tmp/runner.sh',
        'daemon /tmp/runner.sh',
    ]
    full_command = " && ".join(commands)
    admin_client.juju('ssh', ('0', '--', full_command))

    # create rrd database.
    # values for insert, query, update, delete. (then maybe vsize, res?
    # netin/netout)
    # for line in stat_json:
    #     convert line to epoch seconds


def parse_args(argv):
    """Parse all arguments."""
    parser = argparse.ArgumentParser(description="Simple perf/scale testing")
    add_basic_testing_arguments(parser)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)
    bs_manager = BootstrapManager.from_args(args)
    assess_perf_test_simple(bs_manager, args.upload_tools)

    # # # deploy
    # deploy = TimingData(
    #     datetime(2016, 7, 19, 2, 40, 27, 855692),
    #     datetime(2016, 7, 19, 2, 51, 51, 910747))
    # deploy2 = TimingData(
    #     datetime(2016, 7, 19, 2, 51, 59, 910747),
    #     datetime(2016, 7, 19, 3, 2, 30, 910747))
    # # bootstrap:
    # bootstrap = TimingData(
    #     datetime(2016, 7, 19, 2, 33, 41, 5351),
    #     datetime(2016, 7, 19, 2, 39, 45, 16832))
    # timings = dict(
    #     bootstrap=bootstrap,
    #     deploys=[
    #         DeployDetails('blah', dict(), deploy),
    #         DeployDetails('blah2', dict(), deploy2)
    #     ],
    #     cleanup=bootstrap)
    # generate_reports('', '/tmp/example_collection/', timings)

    return 0


if __name__ == '__main__':
    sys.exit(main())
