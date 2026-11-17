"""Fail closed: only a registered, precisely observed bug may be expected.

Tests raise KnownGapFailure only after observing the existing public bug signature.
Ordinary assertions, fixture/setup errors, and newly fixed gaps fail the baseline.
"""
import unittest


# 真实注册表已清空：八项缺陷全部修复，基线不再豁免任何失败。
# 严格门禁本身仍需可验证，元测试改用 tests.baseline.synthetic_gaps 的合成登记表。
KNOWN_GAPS = {}


def known_gap(gap_id, registry=None):
    registered = KNOWN_GAPS if registry is None else registry
    if gap_id not in registered:
        raise ValueError("Unregistered known-gap ID: " + str(gap_id))

    def decorate(function):
        if hasattr(function, "__known_gap_id__"):
            raise ValueError("A test may identify only one known gap")
        function.__known_gap_id__ = gap_id
        return function

    return decorate


class KnownGapFailure(AssertionError):
    def __init__(self, gap_id, message):
        super().__init__(message)
        self.gap_id = gap_id


class StrictGapResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity, registry=None):
        super().__init__(stream, descriptions, verbosity)
        self.registry = KNOWN_GAPS if registry is None else registry
        self.gap_records = []

    @staticmethod
    def _method(test):
        return getattr(test, getattr(test, "_testMethodName", ""), None)

    def _tag(self, test):
        tag = getattr(self._method(test), "__known_gap_id__", None)
        return tag if isinstance(tag, str) and tag in self.registry else None

    def _matching_marker(self, test, err):
        tag = self._tag(test)
        if not tag or not isinstance(err[1], KnownGapFailure) or err[1].gap_id != tag:
            return False
        # Even a fixture delegating to the tagged method must remain a real failure.
        method_code = getattr(self._method(test), "__code__", None)
        fixture_codes = {unittest.TestCase._callSetUp.__code__,
                         unittest.TestCase._callTearDown.__code__,
                         unittest.TestCase._callCleanup.__code__}
        test_call_code = unittest.TestCase._callTestMethod.__code__
        observed_method = observed_test_call = False
        traceback = err[2]
        while traceback is not None:
            code = traceback.tb_frame.f_code
            if code in fixture_codes:
                return False
            observed_method |= code is method_code
            observed_test_call |= code is test_call_code
            traceback = traceback.tb_next
        return observed_method and observed_test_call

    def _record(self, test, outcome, reason):
        tag = self._tag(test)
        if tag:
            self.gap_records.append({"id": tag, "test": str(test.id()),
                                     "outcome": outcome, "reason": str(reason)})

    def addFailure(self, test, err):
        if self._matching_marker(test, err):
            self._record(test, "expected_failure", err[1])
            return super().addExpectedFailure(test, err)
        self._record(test, "failure", err[1])
        return super().addFailure(test, err)

    def addError(self, test, err):
        self._record(test, "error", type(err[1]).__name__ + ": " + str(err[1]))
        return super().addError(test, err)

    def addSuccess(self, test):
        if self._tag(test):
            return self.addUnexpectedSuccess(test)
        return super().addSuccess(test)

    def addUnexpectedSuccess(self, test):
        self._record(test, "unexpected_success", "Registered bug signature no longer reproduced")
        return super().addUnexpectedSuccess(test)

    def addExpectedFailure(self, test, err):
        # Do not let unittest.expectedFailure bypass the strict marker check.
        if isinstance(err[1], test.failureException):
            return self.addFailure(test, err)
        return self.addError(test, err)
