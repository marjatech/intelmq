# SPDX-FileCopyrightText: 2015 Sebastian Wagner
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""
Utilities for testing intelmq bots.

The BotTestCase can be used as base class for unittests on bots. It includes
some basic generic tests (logged errors, correct pipeline setup).
"""
import io
import inspect
import json
import os
import re
import unittest
import unittest.mock as mock
from itertools import chain

import pkg_resources
import redis

import intelmq.lib.message as message
import intelmq.lib.pipeline as pipeline
import intelmq.lib.utils as utils
from intelmq import CONFIG_DIR, RUNTIME_CONF_FILE

__all__ = ['BotTestCase']

BOT_CONFIG = {"destination_pipeline_broker": "pythonlist",
              "logging_handler": "stream",
              "logging_path": None,
              "logging_level": "DEBUG",
              "rate_limit": 0,
              "retry_delay": 0,
              "error_retry_delay": 0,
              "error_max_retries": 0,
              "redis_cache_host": os.getenv('INTELMQ_PIPELINE_HOST', 'localhost'),
              "redis_cache_port": 6379,
              "redis_cache_db": 4,
              "redis_cache_ttl": 10,
              "redis_cache_password": os.environ.get('INTELMQ_TEST_REDIS_PASSWORD'),
              "source_pipeline_broker": "pythonlist",
              "testing": True,
              }
BOT_INIT_REGEX = ("{} initialized with id {} and"
                  r" intelmq [0-9a-z.]*"
                  r" and python [0-9a-z.]*"
                  r" \(.*?\)([0-9a-zA-Z.\ \[\]]*)"
                  r"as process [0-9]+\.")


class Parameters:
    pass


def mocked_config(bot_id='test-bot', sysconfig={}, group=None, module=None):
    def mocked(conf_file):
        if conf_file == RUNTIME_CONF_FILE:
            return {bot_id: {'description': 'Instance of a bot for automated unit tests.',
                             'group': group,
                             'module': module,
                             'name': 'Test Bot',
                             'parameters': sysconfig,
                             }}
        elif conf_file.startswith(CONFIG_DIR):
            confname = os.path.join('etc/', os.path.split(conf_file)[-1])
            fname = pkg_resources.resource_filename('intelmq',
                                                    confname)
            with open(fname) as fpconfig:
                return json.load(fpconfig)
        else:
            return utils.load_configuration(conf_file)

    return mocked


def mocked_get_global_settings():
    return BOT_CONFIG


def skip_database():
    return unittest.skipUnless(os.environ.get('INTELMQ_TEST_DATABASES'),
                               'Skipping database tests.')


def skip_internet():
    return unittest.skipIf(os.environ.get('INTELMQ_SKIP_INTERNET'),
                           'Skipping without internet connection.')


def skip_redis():
    return unittest.skipIf(os.environ.get('INTELMQ_SKIP_REDIS'),
                           'Skipping without running redis.')


def skip_exotic():
    return unittest.skipUnless(os.environ.get('INTELMQ_TEST_EXOTIC'),
                               'Skipping tests requiring exotic libs.')


def skip_ci():
    return unittest.skipIf(os.getenv('CI') == 'true' or os.environ.get('DEB_BUILD_ARCH'),
                           'Test disabled on CI.')


def skip_build_environment():
    # For test that regularly fail in build environments like local or public Open Build Service builds
    return unittest.skipIf(os.getenv('USER') == 'abuild', 'Test disabled in Build Service.')


class BotTestCase:
    """
    Provides common tests and assert methods for bot testing.
    """

    bot_types = {'collector': 'CollectorBot',
                 'parser': 'ParserBot',
                 'expert': 'ExpertBot',
                 'output': 'OutputBot',
                 }

    @classmethod
    def setUpClass(cls):
        """
        Set default values and save original functions.
        """
        if not utils.drop_privileges():
            raise ValueError('IntelMQ and IntelMQ tests must not run as root for security reasons. '
                             'Dropping privileges did not work.')

        cls.bot_id = 'test-bot'
        cls.bot_name = None
        cls.bot = None
        cls.bot_reference = None
        cls.bot_type = None
        cls.default_input_message = ''
        cls.input_message = None
        cls.loglines = []
        cls.loglines_buffer = ''
        cls.log_stream = None
        cls.maxDiff = None  # For unittest module, prints long diffs
        cls.pipe = None
        cls.sysconfig = {}
        cls.use_cache = False
        cls.allowed_warning_count = 0
        cls.allowed_error_count = 0  # allows dumping of some lines

        cls.set_bot()

        cls.bot_name = cls.bot_reference.__name__
        if cls.bot_type is None:
            for type_name, type_match in cls.bot_types.items():
                if cls.bot_name.endswith(type_match):
                    cls.bot_type = type_name
                    break
        if cls.bot_type == 'parser' and cls.default_input_message == '':
            cls.default_input_message = {'__type': 'Report',
                                         'raw': 'Cg==',
                                         'feed.name': 'Test Feed',
                                         'time.observation': '2016-01-01T00:00:00+00:00'}
        elif cls.bot_type != 'collector' and cls.default_input_message == '':
            cls.default_input_message = {'__type': 'Event'}
        if type(cls.default_input_message) is dict:
            cls.default_input_message = \
                utils.decode(json.dumps(cls.default_input_message))

        if cls.use_cache and not os.environ.get('INTELMQ_SKIP_REDIS'):
            password = os.environ.get('INTELMQ_TEST_REDIS_PASSWORD') or \
                (BOT_CONFIG['redis_cache_password'] if 'redis_cache_password' in BOT_CONFIG else None)
            cls.cache = redis.Redis(host=BOT_CONFIG['redis_cache_host'],
                                    port=BOT_CONFIG['redis_cache_port'],
                                    db=BOT_CONFIG['redis_cache_db'],
                                    socket_timeout=BOT_CONFIG['redis_cache_ttl'],
                                    password=password,
                                    )
        elif cls.use_cache and os.environ.get('INTELMQ_SKIP_REDIS'):
            cls.skipTest(cls, 'Requested cache requires deactivated Redis.')

    harmonization = utils.load_configuration(pkg_resources.resource_filename('intelmq',
                                                                             'etc/harmonization.conf'))

    def new_report(self, auto=False, examples=False):
        return message.Report(harmonization=self.harmonization, auto=auto)

    def new_event(self):
        return message.Event(harmonization=self.harmonization)

    def get_mocked_logger(self, logger):
        def log(name, *args, **kwargs):
            logger.handlers = self.logger_handlers_backup
            return logger
        return log

    def prepare_bot(self, parameters={}, destination_queues=None, prepare_source_queue: bool = True):
        """
        Reconfigures the bot with the changed attributes.

        Parameters:
            parameters: optional bot parameters for this run, as dict
            destination_queues: optional definition of destination queues
                default: {"_default": "{}-output".format(self.bot_id)}
        """
        self.log_stream = io.StringIO()

        src_name = f"{self.bot_id}-input"
        if not destination_queues:
            destination_queues = {"_default": f"{self.bot_id}-output"}
        else:
            destination_queues = {queue_name: f"{self.bot_id}-{queue_name.strip('_')}-output"
                                  for queue_name in destination_queues}

        config = BOT_CONFIG.copy()
        config.update(self.sysconfig)
        config.update(parameters)
        config['destination_queues'] = destination_queues
        self.mocked_config = mocked_config(self.bot_id,
                                           sysconfig=config,
                                           group=self.bot_type.title(),
                                           module=self.bot_reference.__module__,
                                           )

        self.logger = utils.log(self.bot_id,
                                log_path=False, stream=self.log_stream,
                                log_format_stream=utils.LOG_FORMAT,
                                log_level=config['logging_level'])
        self.logger_handlers_backup = self.logger.handlers

        parameters = Parameters()
        setattr(parameters, 'source_queue', src_name)
        setattr(parameters, 'destination_queues', destination_queues)

        with mock.patch('intelmq.lib.utils.load_configuration',
                        new=self.mocked_config):
            with mock.patch('intelmq.lib.utils.log', self.get_mocked_logger(self.logger)):
                with mock.patch('intelmq.lib.utils.get_global_settings', mocked_get_global_settings):
                    self.bot = self.bot_reference(self.bot_id)
        self.bot._Bot__stats_cache = None

        pipeline_args = {key: getattr(self, key) for key in dir(self) if not inspect.ismethod(getattr(self, key)) and (key.startswith('source_pipeline_') or key.startswith('destination_pipeline'))}
        self.pipe = pipeline.Pythonlist(logger=self.logger, pipeline_args=pipeline_args, load_balance=self.bot.load_balance, is_multithreaded=self.bot.is_multithreaded)
        self.pipe.set_queues(parameters.source_queue, "source")
        self.pipe.set_queues(parameters.destination_queues, "destination")

        if prepare_source_queue:
            self.prepare_source_queue()

    def prepare_source_queue(self):
        if self.input_message is not None:
            if not isinstance(self.input_message, (list, tuple)):
                self.input_message = [self.input_message]
            self.input_queue = []
            for msg in self.input_message:
                if type(msg) is dict:
                    self.input_queue.append(json.dumps(msg))
                elif issubclass(type(msg), message.Message):
                    self.input_queue.append(msg.serialize())
                else:
                    self.input_queue.append(msg)
            self.input_message = None
        else:
            if self.default_input_message:  # None for collectors
                self.input_queue = [self.default_input_message]

    def test_static_bot_check_method(self, *args, **kwargs):
        """
        Check if the bot's static check() method completes without errors (exceptions).
        The return value (errors) are *not* checked.

        The arbitrary parameters for this test function are needed because if a
        mocker mocks the test class, parameters can be added.
        See for example `intelmq.tests.bots.collectors.http.test_collector`.
        """
        checks = self.bot_reference.check(self.sysconfig)
        if checks is None:
            return
        self.assertIsInstance(checks, (list, tuple))
        for check in checks:
            self.assertIsInstance(check, (list, tuple),
                                  '%s.check returned an invalid format. '
                                  'Return value must be a sequence of sequences.'
                                  '' % self.bot_name)
            self.assertEqual(len(check), 2,
                             '%s.check returned an invalid format. '
                             'Return value\'s inner sequence must have a length of 2.'
                             '' % self.bot_name)
            self.assertNotEqual(check[0].upper(), 'ERROR',
                                '%s.check returned the error %r.'
                                '' % (self.bot_name, check[1]))
        raise ValueError(f'checks is {checks!r}')

    def run_bot(self, iterations: int = 1, error_on_pipeline: bool = False,
                prepare=True, parameters={},
                allowed_error_count=0,
                allowed_warning_count=0,
                stop_bot: bool = True):
        """
        Call this method for actually doing a test run for the specified bot.

        Parameters:
            iterations: Bot instance will be run the given times, defaults to 1.
            parameters: passed to prepare_bot
            allowed_error_count: maximum number allow allowed errors in the logs
            allowed_warning_count: maximum number allow allowed warnings in the logs
            bot_stop: If the bot should be stopped/shut down after running it. Set to False, if you are calling this method again afterwards, as the bot shutdown destroys structures (pipeline, etc.)
        """
        if prepare:
            self.prepare_bot(parameters=parameters)
        elif parameters:
            raise ValueError("Parameter 'parameters' is given, but parameter "
                             "'prepare' is false. Parameters must be passed on "
                             "to 'prepare_bot' to be effective.")
        with mock.patch('intelmq.lib.utils.load_configuration',
                        new=self.mocked_config):
            with mock.patch('intelmq.lib.utils.log', self.get_mocked_logger(self.logger)):
                for run in range(iterations):
                    self.bot.start(error_on_pipeline=error_on_pipeline,
                                   source_pipeline=self.pipe,
                                   destination_pipeline=self.pipe)
                if stop_bot:
                    self.bot.stop(exitcode=0)
        self.loglines_buffer = self.log_stream.getvalue()
        self.loglines = self.loglines_buffer.splitlines()

        """ Test if input queue is empty. """
        self.assertEqual(self.input_queue, [],
                         'Not all input messages have been processed. '
                         'You probably need to increase the number of '
                         'iterations of `run_bot`.')

        internal_queue_size = len(self.get_input_internal_queue())
        self.assertEqual(internal_queue_size, 0,
                         'The internal input queue is not empty, but has '
                         f'{internal_queue_size} element(s). '
                         'The bot did not acknowledge all messages.')

        """ Test if report has required fields. """
        if self.bot_type == 'collector':
            for report_json in self.get_output_queue():
                report = message.MessageFactory.unserialize(report_json,
                                                            harmonization=self.harmonization)
                self.assertIsInstance(report, message.Report)
                self.assertIn('raw', report)
                self.assertIn('time.observation', report)

        """ Test if event has required fields. """
        if self.bot_type == 'parser':
            for event_json in self.get_output_queue():
                event = message.MessageFactory.unserialize(event_json,
                                                           harmonization=self.harmonization)
                self.assertIsInstance(event, message.Event)
                self.assertIn('classification.type', event)
                self.assertIn('raw', event)

        """ Test if bot log messages are correctly formatted. """
        self.assertLoglineMatches(0, BOT_INIT_REGEX.format(self.bot_name,
                                                           self.bot_id), "INFO")
        self.assertRegexpMatchesLog("INFO - Bot is starting.")
        if stop_bot:
            self.assertLoglineEqual(-1, "Bot stopped.", "INFO")

        allowed_error_count = max(allowed_error_count, self.allowed_error_count)
        self.assertLessEqual(len(re.findall(' - ERROR - ', self.loglines_buffer)), allowed_error_count)
        allowed_warning_count = max(allowed_warning_count, self.allowed_warning_count)
        self.assertLessEqual(len(re.findall(' - WARNING - ', self.loglines_buffer)), allowed_warning_count)
        self.assertNotRegexpMatchesLog("CRITICAL")
        """ If no error happened (incl. tracebacks) we can check for formatting """
        if not self.allowed_error_count:
            for logline in self.loglines:
                fields = utils.parse_logline(logline)
                if not isinstance(fields, dict):
                    # Traceback
                    continue
                self.assertTrue(fields['message'][-1] in '.:?!',
                                msg='Logline {!r} does not end with .? or !.'
                                    ''.format(fields['message']))
                self.assertTrue(fields['message'].upper() == fields['message'].upper(),
                                msg='Logline {!r} does not begin with an upper case char.'
                                    ''.format(fields['message']))

    @classmethod
    def tearDownClass(cls):
        if cls.use_cache and not os.environ.get('INTELMQ_SKIP_REDIS'):
            cls.cache.flushdb()

    def get_input_queue(self):
        """Returns the input queue of this bot which can be filled
           with fixture data in setUp()"""
        if self.pipe:
            return self.pipe.state["%s-input" % self.bot_id]
        else:
            return []

    def get_input_internal_queue(self):
        """Returns the internal input queue of this bot which can be filled
           with fixture data in setUp()"""
        if self.pipe:
            return self.pipe.state["%s-input-internal" % self.bot_id]
        else:
            return []

    def set_input_queue(self, seq):
        """Setter for the input queue of this bot"""
        self.pipe.state["%s-input" % self.bot_id] = [utils.encode(text) for
                                                     text in seq]

    input_queue = property(get_input_queue, set_input_queue)

    def get_output_queue(self, path="_default"):
        """Getter for items in the output queues of this bot. Use in TestCase scenarios
            If there is multiple queues in named queue group, we return all the items chained.
        """
        return [utils.decode(text) for text in chain(*[self.pipe.state[x] for x in self.pipe.destination_queues[path]])]
        # return [utils.decode(text) for text in self.pipe.state["%s-output" % self.bot_id]]

    def test_bot_name(self, *args, **kwargs):
        """
        Test if Bot has a valid name.
        Must be CamelCase and end with CollectorBot etc.

        Accept arbitrary arguments in case the test methods get mocked
        and get some additional arguments. All arguments are ignored.
        """
        counter = 0
        for type_name, type_match in self.bot_types.items():
            try:
                self.assertRegex(self.bot_name,
                                 fr'\A[a-zA-Z0-9]+{type_match}\Z')
            except AssertionError:
                counter += 1
        if counter != len(self.bot_types) - 1:
            self.fail("Bot name {!r} does not match one of {!r}"
                      "".format(self.bot_name, list(self.bot_types.values())))  # pragma: no cover

    def assertAnyLoglineEqual(self, message: str, levelname: str = "ERROR"):
        """
        Asserts if any logline matches a specific requirement.

        Parameters:
            message: Message text which is compared
            type: Type of logline which is asserted

        Raises:
            ValueError: if logline message has not been found
        """

        self.assertIsNotNone(self.loglines)
        for logline in self.loglines:
            fields = utils.parse_logline(logline)

            if levelname == fields["log_level"] and message == fields["message"]:
                return
        else:
            raise ValueError('Logline with level {!r} and message {!r} not found'
                             ''.format(levelname, message))  # pragma: no cover

    def assertLoglineEqual(self, line_no: int, message: str, levelname: str = "ERROR"):
        """
        Asserts if a logline matches a specific requirement.

        Parameters:
            line_no: Number of the logline which is asserted
            message: Message text which is compared
            levelname: Log level of logline which is asserted
        """
        self.assertIsNotNone(self.loglines)
        logline = self.loglines[line_no]
        fields = utils.parse_logline(logline)

        self.assertEqual(self.bot_id, fields["bot_id"],
                         "bot_id {!r} didn't match {!r}."
                         "".format(self.bot_id, fields["bot_id"]))

        self.assertEqual(levelname, fields["log_level"])
        self.assertEqual(message, fields["message"])

    def assertLoglineMatches(self, line_no: int, pattern: str, levelname: str = "ERROR"):
        """
        Asserts if a logline matches a specific requirement.

        Parameters:
            line_no: Number of the logline which is asserted
            pattern: Message text which is compared
            type: Type of logline which is asserted
        """

        self.assertIsNotNone(self.loglines)
        logline = self.loglines[line_no]
        fields = utils.parse_logline(logline)

        self.assertEqual(self.bot_id, fields["bot_id"],
                         "bot_id {!r} didn't match {!r}."
                         "".format(self.bot_id, fields["bot_id"]))

        self.assertEqual(levelname, fields["log_level"])
        self.assertRegex(fields["message"], pattern)

    def assertLogMatches(self, pattern: str, levelname: str = "ERROR"):
        """
        Asserts if any logline matches a specific requirement.

        Parameters:
            pattern: Message text which is compared, regular expression.
            levelname: Log level of the logline which is asserted, upper case.
        """
        self.assertIsNotNone(self.loglines)
        for logline in self.loglines:
            fields = utils.parse_logline(logline)

            #  Exception tracebacks
            if isinstance(fields, str):
                if levelname == "ERROR" and re.match(pattern, fields):
                    break
            elif levelname == fields["log_level"] and re.match(pattern, fields["message"]):
                break
        else:
            raise ValueError('No matching logline found.')  # pragma: no cover

    def assertRegexpMatchesLog(self, pattern):
        """Asserts that pattern matches against log. """

        self.assertIsNotNone(self.loglines_buffer)
        self.assertRegex(self.loglines_buffer, pattern)

    def assertNotRegexpMatchesLog(self, pattern):
        """Asserts that pattern doesn't match against log."""

        self.assertIsNotNone(self.loglines_buffer)
        self.assertNotRegex(self.loglines_buffer, pattern)

    def assertOutputQueueLen(self, queue_len=0, path="_default"):
        """
        Asserts that the output queue has the expected length.
        """
        self.assertEqual(len(self.get_output_queue(path=path)), queue_len)

    def assertMessageEqual(self, queue_pos, expected_msg, compare_raw=True, path="_default"):
        """
        Asserts that the given expected_message is
        contained in the generated event with
        given queue position.
        """
        event = self.get_output_queue(path=path)[queue_pos]
        self.assertIsInstance(event, str)

        event_dict = json.loads(event)
        if isinstance(expected_msg, (message.Event, message.Report)):
            expected = expected_msg.to_dict(with_type=True)
        else:
            expected = expected_msg.copy()

        if not compare_raw:
            expected.pop('raw', None)
            event_dict.pop('raw', None)
        if 'time.observation' in event_dict:
            del event_dict['time.observation']
        if 'time.observation' in expected:
            del expected['time.observation']
        if 'output' in event_dict:
            event_dict['output'] = json.loads(event_dict['output'])
        if 'output' in expected:
            expected['output'] = json.loads(expected['output'])

        self.assertDictEqual(expected, event_dict)

    def tearDown(self):
        """
        Check if the bot did consume all messages.

        Executed after every test run.
        """
        self.assertEqual(len(self.input_queue), 0)
