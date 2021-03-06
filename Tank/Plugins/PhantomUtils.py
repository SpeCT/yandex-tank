""" Utility classes for phantom module """
# TODO: use separate answ log per benchmark
import copy
import logging
import multiprocessing
import os
import re
import socket
import string

from Tank.stepper import StepperWrapper


class PhantomConfig:
    """ config file generator """
    OPTION_PHOUT = "phout_file"
    SECTION = 'phantom'

    def __init__(self, core):
        self.core = core
        self.log = logging.getLogger(__name__)
        self.streams = []

        # common
        self.timeout = -1
        self.answ_log = None
        self.answ_log_level = None
        self.phout_file = None
        self.stat_log = None
        self.phantom_log = None
        self.phantom_start_time = None
        self.phantom_modules_path = None
        self.threads = None
        self.additional_libs = None

    def get_option(self, opt_name, default=None):
        """ get option wrapper """
        return self.core.get_option(self.SECTION, opt_name, default)

    @staticmethod
    def get_available_options():
        opts = ["threads", "phantom_modules_path",
                "additional_libs", "writelog", ]
        opts += StreamConfig.get_available_options()
        return opts

    def read_config(self):
        """        Read phantom tool specific options        """
        self.threads = self.get_option(
            "threads", str(int(multiprocessing.cpu_count() / 2) + 1))
        self.phantom_modules_path = self.get_option(
            "phantom_modules_path", "/usr/lib/phantom")
        self.additional_libs = self.get_option("additional_libs", "")
        self.answ_log_level = self.get_option("writelog", "none")
        if self.answ_log_level == '0':
            self.answ_log_level = 'none'
        elif self.answ_log_level == '1':
            self.answ_log_level = 'all'

        self.answ_log = self.core.mkstemp(".log", "answ_")
        self.core.add_artifact_file(self.answ_log)
        self.phout_file = self.core.get_option(self.SECTION, self.OPTION_PHOUT, '')
        if not self.phout_file:
            self.phout_file = self.core.mkstemp(".log", "phout_")
            self.core.set_option(self.SECTION, self.OPTION_PHOUT, self.phout_file)
        self.core.add_artifact_file(self.phout_file)
        self.stat_log = self.core.mkstemp(".log", "phantom_stat_")
        self.core.add_artifact_file(self.stat_log)
        self.phantom_log = self.core.mkstemp(".log", "phantom_")
        self.core.add_artifact_file(self.phantom_log)

        main_stream = StreamConfig(
            self.core, len(self.streams), self.phout_file,
            self.answ_log, self.answ_log_level, self.timeout, self.SECTION)
        self.streams.append(main_stream)

        for section in self.core.config.find_sections(self.SECTION + '-'):
            self.streams.append(
                StreamConfig(self.core, len(self.streams), self.phout_file, self.answ_log, self.answ_log_level,
                             self.timeout, section))

        for stream in self.streams:
            stream.read_config()

    def compose_config(self):
        """        Generate phantom tool run config        """
        streams_config = ''
        stat_benchmarks = ''
        for stream in self.streams:
            streams_config += stream.compose_config()
            if stream.section != self.SECTION:
                stat_benchmarks += " " + "benchmark_io%s" % stream.sequence_no

        kwargs = {}
        kwargs['threads'] = self.threads
        kwargs['phantom_log'] = self.phantom_log
        kwargs['stat_log'] = self.stat_log
        kwargs['benchmarks_block'] = streams_config
        kwargs['stat_benchmarks'] = stat_benchmarks
        kwargs['additional_libs'] = self.additional_libs
        kwargs['phantom_modules_path'] = self.phantom_modules_path
        filename = self.core.mkstemp(".conf", "phantom_")
        self.core.add_artifact_file(filename)
        self.log.debug("Generating phantom config: %s", filename)
        tpl_file = open(os.path.dirname(__file__) + "/phantom/phantom.conf.tpl", 'r')
        template_str = tpl_file.read()
        tpl_file.close()
        tpl = string.Template(template_str)
        config = tpl.substitute(kwargs)

        handle = open(filename, 'w')
        handle.write(config)
        handle.close()
        return filename

    def set_timeout(self, timeout):
        """ pass timeout to all streams """
        for stream in self.streams:
            stream.timeout = timeout

    def get_info(self):
        """ get merged info about phantom conf """
        result = copy.copy(self.streams[0])
        result.stat_log = self.stat_log
        result.steps = []
        result.ammo_file = ''
        result.rps_schedule = None
        result.ammo_count = 0
        result.duration = 0

        result.instances = 0
        result.loadscheme = []
        result.loop_count = 0

        for stream in self.streams:
            sec_no = 0
            logging.debug("Steps: %s", stream.stepper_wrapper.steps)
            for item in stream.stepper_wrapper.steps:
                for x in range(0, item[1]):
                    if len(result.steps) > sec_no:
                        result.steps[sec_no][0] += item[0]
                    else:
                        result.steps.append([item[0], 1])
                    sec_no += 1

            if result.rps_schedule:
                result.rps_schedule = []
            else:
                result.rps_schedule = stream.stepper_wrapper.loadscheme
            if result.loadscheme:
                result.loadscheme = ''
            else:
                # FIXME: add formatted load scheme for server:
                # <step_size,step_type,first_rps,last_rps,original_step_params>
                # as a string
                result.loadscheme = ''

            if result.loop_count:
                result.loop_count = u'0'
            else:
                result.loop_count = stream.stepper_wrapper.loop_count

            result.ammo_file += stream.stepper_wrapper.ammo_file + ' '
            result.ammo_count += stream.stepper_wrapper.ammo_count
            result.duration = max(
                result.duration, stream.stepper_wrapper.duration)
            result.instances += stream.instances

        if not result.ammo_count:
            raise ValueError("Total ammo count cannot be zero")
        return result


class StreamConfig:
    """ each test stream's config """

    OPTION_INSTANCES_LIMIT = 'instances'

    def __init__(self, core, sequence, phout, answ, answ_level, timeout, section):
        self.core = core
        self.address_wizard = AddressWizard()

        self.sequence_no = sequence
        self.log = logging.getLogger(__name__)
        self.section = section
        self.stepper_wrapper = StepperWrapper(self.core, self.section)
        self.phout_file = phout
        self.answ_log = answ
        self.answ_log_level = answ_level
        self.timeout = timeout

        # per benchmark
        self.instances = None
        self.ipv6 = None
        self.ssl = None
        self.address = None
        self.port = None
        self.tank_type = None
        self.stpd = None
        self.gatling = None
        self.phantom_http_line = None
        self.phantom_http_field_num = None
        self.phantom_http_field = None
        self.phantom_http_entity = None
        self.resolved_ip = None
        self.method_prefix = None
        self.source_log_prefix = None
        self.method_options = None

    def get_option(self, option_ammofile, default=None):
        """ get option wrapper """
        return self.core.get_option(self.section, option_ammofile, default)

    @staticmethod
    def get_available_options():
        opts = ["ssl", "tank_type", 'gatling_ip',
                "method_prefix", "source_log_prefix"]
        opts += ["phantom_http_line", "phantom_http_field_num",
                 "phantom_http_field", "phantom_http_entity"]
        opts += ['address', "port", StreamConfig.OPTION_INSTANCES_LIMIT]
        opts += StepperWrapper.get_available_options()
        opts += ["connection_test"]
        return opts

    def read_config(self):
        """ reads config """
        # multi-options
        self.ssl = int(self.get_option("ssl", '0'))
        self.tank_type = self.get_option("tank_type", 'http')
        # TODO: refactor. Maybe we should decide how to interact with StepperWrapper here.
        self.instances = int(
            self.get_option(self.OPTION_INSTANCES_LIMIT, '1000'))
        self.gatling = ' '.join(self.get_option('gatling_ip', '').split("\n"))
        self.method_prefix = self.get_option("method_prefix", 'method_stream')
        self.method_options = self.get_option("method_options", '')
        self.source_log_prefix = self.get_option("source_log_prefix", '')

        self.phantom_http_line = self.get_option("phantom_http_line", "")
        self.phantom_http_field_num = self.get_option("phantom_http_field_num", "")
        self.phantom_http_field = self.get_option("phantom_http_field", "")
        self.phantom_http_entity = self.get_option("phantom_http_entity", "")

        self.address = self.get_option('address', '127.0.0.1')
        self.ipv6, self.resolved_ip, self.port, self.address = self.address_wizard.resolve(self.address)

        if self.port is None:
            explicit_port = self.get_option('port', '')
            if explicit_port:
                self.log.warn("Using phantom.port option is deprecated. Use phantom.address=[address]:port instead")
                self.port = int(explicit_port)
            else:
                self.port = 80

        logging.info("Resolved %s into %s:%s", self.address, self.resolved_ip, self.port)

        if int(self.get_option("connection_test", "1")):
            self.address_wizard.test(self.resolved_ip, self.port)

        self.stepper_wrapper.read_config()

    def compose_config(self):
        """ compose benchmark block """
        # step file
        self.stepper_wrapper.prepare_stepper()
        self.stpd = self.stepper_wrapper.stpd
        if self.stepper_wrapper.instances:
            self.instances = self.stepper_wrapper.instances

        if not self.stpd:
            raise RuntimeError("Cannot proceed with no STPD file")

        kwargs = {}
        kwargs['sequence_no'] = self.sequence_no
        kwargs[
            'ssl_transport'] = "transport_t ssl_transport = transport_ssl_t { timeout = 1s }\n transport = ssl_transport" if self.ssl else ""
        kwargs['method_stream'] = self.method_prefix + \
                                  "_ipv6_t" if self.ipv6 else self.method_prefix + "_ipv4_t"
        kwargs['phout'] = self.phout_file
        kwargs['answ_log'] = self.answ_log
        kwargs['answ_log_level'] = self.answ_log_level
        kwargs['comment_answ'] = "# " if self.answ_log_level == 'none' else ''
        kwargs['stpd'] = self.stpd
        kwargs['source_log_prefix'] = self.source_log_prefix
        kwargs['method_options'] = self.method_options
        if self.tank_type:
            kwargs[
                'proto'] = "proto=http_proto%s" % self.sequence_no if self.tank_type == 'http' else "proto=none_proto"
            kwargs['comment_proto'] = ""
        else:
            kwargs['proto'] = ""
            kwargs['comment_proto'] = "#"

        if self.gatling:
            kwargs['bind'] = 'bind={ ' + self.gatling + ' }'
        else:
            kwargs['bind'] = ''
        kwargs['ip'] = self.resolved_ip
        kwargs['port'] = self.port
        kwargs['timeout'] = self.timeout
        kwargs['instances'] = self.instances
        tune = ''
        if self.phantom_http_entity:
            tune += "entity = " + self.phantom_http_entity + "\n"
        if self.phantom_http_field:
            tune += "field = " + self.phantom_http_field + "\n"
        if self.phantom_http_field_num:
            tune += "field_num = " + self.phantom_http_field_num + "\n"
        if self.phantom_http_line:
            tune += "line = " + self.phantom_http_line + "\n"
        if tune:
            kwargs['reply_limits'] = 'reply_limits = {\n' + tune + "}"
        else:
            kwargs['reply_limits'] = ''

        if self.section == PhantomConfig.SECTION:
            fname = 'phantom_benchmark_main.tpl'
        else:
            fname = 'phantom_benchmark_additional.tpl'
        tplf = open(os.path.dirname(__file__) + '/phantom/' + fname, 'r')
        template_str = tplf.read()
        tplf.close()
        tpl = string.Template(template_str)
        config = tpl.substitute(kwargs)

        return config


# ========================================================================

class AddressWizard:
    def __init__(self):
        self.lookup_fn = socket.getaddrinfo
        self.socket_class = socket.socket

    def resolve(self, address_str):
        """

        :param address_str:
        :return: tuple of boolean, string, int - isIPv6, resolved_ip, port (may be null), extracted_address
        """
        logging.debug("Trying to resolve address string")

        port = None

        braceport_pat = "^\[([^]]+)\]:(\d+)$"
        braceonly_pat = "^\[([^]]+)\]$"
        if re.match(braceport_pat, address_str):
            logging.debug("Braces and port present")
            match = re.match(braceport_pat, address_str)
            logging.debug("Match: %s %s ", match.group(1), match.group(2))
            address_str, port = match.group(1), match.group(2)
        elif re.match(braceonly_pat, address_str):
            logging.debug("Braces only present")
            match = re.match(braceonly_pat, address_str)
            logging.debug("Match: %s", match.group(1))
            address_str = match.group(1)
        else:
            logging.debug("Parsing port")
            parts = address_str.split(":")
            if len(parts) <= 2:  # otherwise it is v6 address
                address_str = parts[0]
                if len(parts) == 2:
                    port = parts[1]

        resolved = self.lookup_fn(address_str, port)
        (family, socktype, proto, canonname, sockaddr) = resolved.pop(0)
        is_v6 = family == socket.AF_INET6
        parsed_ip, port = sockaddr[0], sockaddr[1]

        return is_v6, parsed_ip, int(port) if port else None, address_str

    def test(self, resolved_ip, port):
        lookup = socket.getaddrinfo(resolved_ip, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        af, socktype, proto, canonname, sa = lookup.pop(0)
        test_sock = self.socket_class(af, socktype, proto)
        try:
            test_sock.settimeout(5)
            test_sock.connect(sa)
        except Exception, exc:
            msg = "TCP Connection test failed for %s:%s, use phantom.connection_test=0 to disable it"
            raise RuntimeError(msg % (resolved_ip, port))
        finally:
            test_sock.close()
