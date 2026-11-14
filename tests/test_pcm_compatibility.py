"""Compatibility checks for the re-submittable WAV outputs of /clip and /mix.

Each endpoint returns a complete 16-bit PCM WAV document in ``audio``. These
tests re-submit those documents to ``/inspect``, ``/clip`` and ``/mix`` and
compare the format facts and the sample values with independently computed
expectations.

Comparisons are exact: every int16 sample is checked, frame counts are checked
against the full legal extent (including a source at the 30 second input
limit), and no tolerance, truncation or duration reduction is applied.
"""
import array
import base64
import concurrent.futures
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import wave

from audio_workbench.server import AudioServer, Handler
from audio_workbench.waveio import Audio, decode, encode


def saturate(value):
    """Contract from PROJECT_SPEC.md: clamp to int16 after Python round()."""
    return max(-32768, min(32767, round(value)))


def make_samples(rate, channels, frames, seed=0):
    """Build a deterministic, int16-bounded fixture of exactly ``frames`` frames."""
    block_frames = min(frames, rate)
    block = array.array('h')
    for frame in range(block_frames):
        left = ((frame * 97 + seed * 131) % 4001) - 2000
        if channels == 1:
            block.append(left)
        else:
            block.extend((left, -left // 2 if left else 0))
    samples = block * (frames // block_frames)
    samples.extend(block[:(frames % block_frames) * channels])
    return samples


def inspect_facts(rate, channels, samples):
    frames = len(samples) // channels
    return {'sample_rate': rate, 'channels': channels, 'frames': frames,
            'duration_seconds': frames / rate,
            'peak': max(abs(value) for value in samples) / 32768}


def clip_samples(samples, channels, first, last, gain):
    """Expected output of /clip for integer frame bounds (left-closed, right-open)."""
    return array.array('h', (saturate(sample * gain)
                             for sample in samples[first * channels:last * channels]))


def mix_samples(rate, channels, tracks):
    """Expected /mix result; ``tracks`` is [(samples, offset_seconds, gain)] in order."""
    positions = [round(offset * rate) for _, offset, _ in tracks]
    frames = max(position + len(samples) // channels
                 for (samples, _, _), position in zip(tracks, positions))
    sums = [0.0] * (frames * channels)
    for (samples, _, gain), position in zip(tracks, positions):
        start = position * channels
        for index, sample in enumerate(samples):
            sums[start + index] += sample * gain
    return array.array('h', (saturate(value) for value in sums))


class CompatServer(AudioServer):
    # The concurrent test opens more simultaneous connections than the stdlib
    # default listen backlog of 5; connect failures would mask payload mixups.
    request_queue_size = 64


class QuietHandler(Handler):
    def log_message(self, format, *args):
        pass


class PCMCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.server = CompatServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def call(self, path, body):
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.server.server_port}' + path,
            json.dumps(body).encode(), {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def b64(self, samples, rate, channels):
        return base64.b64encode(encode(Audio(rate, channels, samples))).decode('ascii')

    def decode_response(self, response):
        raw = base64.b64decode(response['audio'], validate=True)
        return raw, decode(raw)

    def assert_samples(self, actual, expected):
        """Exact int16 element comparison: no tolerance, no downsampling of the check."""
        self.assertEqual(len(actual), len(expected))
        if actual != expected:
            self.fail(f'samples differ: actual={list(actual[:16])}... '
                      f'expected={list(expected[:16])}...')

    def assert_wav_facts(self, raw, properties, rate, channels, frames):
        """Check the WAV header actually written, not just the returned echo."""
        with wave.open(io.BytesIO(raw), 'rb') as reader:
            self.assertEqual(reader.getcomptype(), 'NONE')
            self.assertEqual(reader.getnchannels(), channels)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), rate)
            self.assertEqual(reader.getnframes(), frames)
            payload = reader.readframes(frames)
        self.assertEqual(len(payload), frames * channels * 2)
        self.assertEqual((properties['sample_rate'], properties['channels'],
                          properties['frames']), (rate, channels, frames))

    def inspect_reentry(self, audio):
        status, result = self.call('/inspect', {'audio': audio})
        self.assertEqual(status, 200)
        return result

    def test_clip_output_reenters_inspect_and_mix(self):
        cases = [
            (8000, 1, 2000, 500, 1500, 2.0),
            (12000, 2, 6000, 1500, 4500, .5),
            (44100, 1, 11025, 0, 11025, 4.0),
            (48000, 2, 24000, 6000, 18000, 1.0),
        ]
        for rate, channels, frames, first, last, gain in cases:
            with self.subTest(rate=rate, channels=channels, gain=gain):
                samples = make_samples(rate, channels, frames, seed=first + channels)
                status, response = self.call('/clip', {
                    'audio': self.b64(samples, rate, channels),
                    'start': first / rate, 'end': last / rate, 'gain': gain})
                self.assertEqual(status, 200)
                raw, clipped = self.decode_response(response)

                expected = clip_samples(samples, channels, first, last, gain)
                self.assertEqual((clipped.rate, clipped.channels, clipped.frames),
                                 (rate, channels, last - first))
                self.assert_samples(clipped.samples, expected)

                properties = inspect_facts(rate, channels, expected)
                self.assertEqual(response['properties'], properties)
                self.assert_wav_facts(raw, response['properties'], rate, channels, last - first)
                self.assertEqual(encode(clipped), raw, 'decoding the response must be lossless')

                # The same WAV bytes must yield the same facts through /inspect.
                self.assertEqual(self.inspect_reentry(response['audio']), properties)

                # Re-submitting the whole document at unity must be an exact no-op.
                _, again = self.call('/clip', {
                    'audio': response['audio'], 'start': 0,
                    'end': (last - first) / rate, 'gain': 1})
                self.assertEqual(again['properties'], properties)
                self.assert_samples(decode(base64.b64decode(again['audio'])).samples, expected)
                mixed_status, mixed = self.call('/mix', {'tracks': [{'audio': response['audio']}]})
                self.assertEqual(mixed_status, 200)
                self.assertEqual(mixed['properties'], properties)
                self.assert_samples(decode(base64.b64decode(mixed['audio'])).samples, expected)

    def test_mix_output_reenters_inspect_clip_and_mix(self):
        cases = [
            (8000, 1, 4000, 2000, 4.0, .25),
            (16000, 2, 8000, 4000, .5, .25),
            (48000, 2, 24000, 12000, 2.0, .25),
        ]
        for rate, channels, frames, partner_frames, gain, offset in cases:
            with self.subTest(rate=rate, channels=channels, gain=gain):
                first = make_samples(rate, channels, frames, seed=3)
                second = make_samples(rate, channels, partner_frames, seed=17)
                status, response = self.call('/mix', {'tracks': [
                    {'audio': self.b64(first, rate, channels), 'gain': 1},
                    {'audio': self.b64(second, rate, channels), 'offset': offset, 'gain': gain}]})
                self.assertEqual(status, 200)
                raw, mixed = self.decode_response(response)

                expected = mix_samples(rate, channels, [(first, 0, 1), (second, offset, gain)])
                expected_frames = round(offset * rate) + partner_frames
                self.assertEqual((mixed.rate, mixed.channels, mixed.frames),
                                 (rate, channels, expected_frames))
                self.assert_samples(mixed.samples, expected)
                self.assert_wav_facts(raw, response['properties'], rate, channels, expected_frames)

                properties = inspect_facts(rate, channels, expected)
                self.assertEqual(response['properties'], properties)
                self.assertEqual(self.inspect_reentry(response['audio']), properties)

                # Whole document through /mix at unity must not move or requantize samples.
                _, again = self.call('/mix', {'tracks': [{'audio': response['audio']}]})
                self.assertEqual(again['properties'], properties)
                self.assert_samples(decode(base64.b64decode(again['audio'])).samples, expected)

                # The mix output is valid input for a further exact clip...
                clip_first, clip_last = rate // 8, rate // 8 + rate // 4
                _, clipped = self.call('/clip', {
                    'audio': response['audio'], 'start': clip_first / rate,
                    'end': clip_last / rate, 'gain': .5})
                self.assertEqual(clipped['properties'],
                                 inspect_facts(rate, channels,
                                               clip_samples(expected, channels, clip_first,
                                                            clip_last, .5)))
                self.assert_samples(decode(base64.b64decode(clipped['audio'])).samples,
                                    clip_samples(expected, channels, clip_first, clip_last, .5))

                # ...and for a further mix whose expected values come from the
                # decoded first output, not from a relaxed approximation.
                third = make_samples(rate, channels, rate // 4, seed=29)
                _, chained = self.call('/mix', {'tracks': [
                    {'audio': response['audio'], 'gain': .75},
                    {'audio': self.b64(third, rate, channels), 'offset': .5, 'gain': 1.5}]})
                chained_expected = mix_samples(
                    rate, channels, [(expected, 0, .75), (third, .5, 1.5)])
                self.assertEqual(chained['properties'],
                                 inspect_facts(rate, channels, chained_expected))
                self.assert_samples(decode(base64.b64decode(chained['audio'])).samples,
                                    chained_expected)

    def test_chained_clip_keeps_exact_frames_and_int16_rounding(self):
        rate, channels, frames = 16000, 1, 16000
        source = make_samples(rate, channels, frames, seed=5)
        _, first = self.call('/clip', {
            'audio': self.b64(source, rate, channels),
            'start': 4000 / rate, 'end': 12000 / rate, 'gain': 2.0})
        stage_one = clip_samples(source, channels, 4000, 12000, 2.0)
        self.assertEqual(first['properties'], inspect_facts(rate, channels, stage_one))

        _, second = self.call('/clip', {
            'audio': first['audio'], 'start': 1000 / rate, 'end': 3000 / rate, 'gain': .5})
        stage_two = clip_samples(stage_one, channels, 1000, 3000, .5)
        second_expected = inspect_facts(rate, channels, stage_two)
        self.assertEqual(second['properties'], second_expected)
        # 2000 frames survived both hops: neither stage shortened the legal range.
        self.assertEqual(second['properties']['frames'], 2000)
        self.assert_samples(decode(base64.b64decode(second['audio'])).samples, stage_two)

        # Values chosen to expose any sample-precision relaxation: saturation
        # boundaries plus ties that Python round() resolves to even.
        boundary = array.array('h', [-32768, -16384, -3, -1, 0, 1, 3, 16384, 32767])
        for gain in (.5, 4.0):
            with self.subTest(gain=gain):
                _, response = self.call('/clip', {
                    'audio': self.b64(boundary, 8000, 1), 'start': 0,
                    'end': len(boundary) / 8000, 'gain': gain})
                expected = array.array('h', (saturate(value * gain) for value in boundary))
                self.assertEqual(response['properties']['frames'], len(boundary))
                self.assert_samples(decode(base64.b64decode(response['audio'])).samples, expected)

    def test_format_conversion_output_reenters_inspect_and_mix(self):
        source = array.array('h', [1000, -2000, 3000, -4000])
        status, response = self.call('/mix', {
            'tracks': [{'audio': self.b64(source, 8000, 1)}],
            'format': {'sample_rate': 16000, 'channels': 2}})
        self.assertEqual(status, 200)
        raw, converted = self.decode_response(response)
        # Hand-computed linear interpolation, including the clamped last frame.
        expected = array.array('h', [
            1000, 1000, -500, -500, -2000, -2000, 500, 500,
            3000, 3000, -500, -500, -4000, -4000, -4000, -4000])
        self.assertEqual((converted.rate, converted.channels, converted.frames), (16000, 2, 8))
        self.assert_samples(converted.samples, expected)
        self.assertEqual(response['properties'], inspect_facts(16000, 2, expected))
        self.assert_wav_facts(raw, response['properties'], 16000, 2, 8)
        self.assertEqual(self.inspect_reentry(response['audio']), response['properties'])

        _, again = self.call('/mix', {
            'tracks': [{'audio': response['audio']}],
            'format': {'sample_rate': 16000, 'channels': 2}})
        self.assertEqual(again['properties'], response['properties'])
        self.assert_samples(decode(base64.b64decode(again['audio'])).samples, expected)

    def test_concurrent_requests_keep_their_own_inputs(self):
        jobs = [self.prepare_concurrent_job(index) for index in range(8)]
        barrier = threading.Barrier(len(jobs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(self.run_concurrent_job, job, barrier) for job in jobs]
            results = [future.result(timeout=60) for future in futures]

        audio_bytes = []
        for job, (first_response, second_response) in zip(jobs, results):
            with self.subTest(index=job['index'], mode=job['mode']):
                if job['mode'] == 3:
                    self.assertEqual(first_response, job['expected_properties'])
                    continue
                final = second_response or first_response
                expected = job['expected_samples']
                raw, decoded = self.decode_response(final)
                self.assertEqual((decoded.rate, decoded.channels, decoded.frames),
                                 (job['rate'], job['channels'], len(expected) // job['channels']))
                self.assert_samples(decoded.samples, expected)
                self.assertEqual(final['properties'],
                                 inspect_facts(job['rate'], job['channels'], expected))
                self.assert_wav_facts(raw, final['properties'], job['rate'],
                                      job['channels'], len(expected) // job['channels'])
                audio_bytes.append(raw)
        self.assertEqual(len(set(audio_bytes)), len(audio_bytes),
                         'concurrent responses must not share or swap payloads')

    def prepare_concurrent_job(self, index):
        rate = (8000, 12000, 16000, 44100)[index % 4]
        channels = 1 + index // 4
        frames = rate // 2
        gain = (1.0, .5, 2.0, 4.0)[index % 4]
        mode = index % 4
        source = make_samples(rate, channels, frames, seed=index + 1)
        partner = make_samples(rate, channels, rate // 4, seed=index + 101)
        job = {'index': index, 'mode': mode, 'rate': rate, 'channels': channels}
        if mode == 0:
            first, last = rate // 4, rate // 2
            job['request'] = {'audio': self.b64(source, rate, channels),
                              'start': first / rate, 'end': last / rate, 'gain': gain}
            job['expected_samples'] = clip_samples(source, channels, first, last, gain)
        elif mode == 1:
            job['request'] = {'tracks': [
                {'audio': self.b64(source, rate, channels), 'gain': gain},
                {'audio': self.b64(partner, rate, channels), 'offset': .25, 'gain': .5}]}
            job['expected_samples'] = mix_samples(
                rate, channels, [(source, 0, gain), (partner, .25, .5)])
        elif mode == 2:
            first, last = rate // 4, rate // 2
            intermediate = clip_samples(source, channels, first, last, 1.0)
            job['request'] = {'audio': self.b64(source, rate, channels),
                              'start': first / rate, 'end': last / rate, 'gain': 1.0}
            job['mix_request'] = {'tracks': [
                {'audio': None, 'offset': .25, 'gain': gain},
                {'audio': self.b64(partner, rate, channels), 'gain': .5}]}
            job['expected_samples'] = mix_samples(
                rate, channels, [(intermediate, .25, gain), (partner, 0, .5)])
        else:
            job['request'] = {'audio': self.b64(source, rate, channels)}
        if mode != 3:
            job['expected_properties'] = inspect_facts(rate, channels, job['expected_samples'])
        else:
            job['expected_properties'] = inspect_facts(rate, channels, source)
        return job

    def run_concurrent_job(self, job, barrier):
        barrier.wait(timeout=30)
        path = '/inspect' if job['mode'] == 3 else ('/mix' if job['mode'] == 1 else '/clip')
        status, first = self.call(path, job['request'])
        self.assertEqual(status, 200)
        if job['mode'] == 2:
            job['mix_request']['tracks'][0]['audio'] = first['audio']
            status, second = self.call('/mix', job['mix_request'])
            self.assertEqual(status, 200)
            return first, second
        return first, None

    def test_maximum_legal_input_duration_is_preserved(self):
        rate, channels, frames = 48000, 2, 48000 * 30
        source = make_samples(rate, channels, frames, seed=7)
        status, response = self.call('/clip', {
            'audio': self.b64(source, rate, channels), 'start': 0, 'end': 30, 'gain': 1})
        self.assertEqual(status, 200)
        raw, clipped = self.decode_response(response)
        # Exactly the full 30 s extent: no silent shortening to fit validation.
        self.assertEqual(clipped.frames, frames)
        self.assertEqual(clipped.rate, rate)
        self.assertEqual(clipped.channels, channels)
        self.assert_samples(clipped.samples, source)
        self.assertEqual(response['properties']['frames'], 1_440_000)
        self.assertEqual(response['properties']['duration_seconds'], 30.0)
        self.assert_wav_facts(raw, response['properties'], rate, channels, frames)

        inspected = self.inspect_reentry(response['audio'])
        self.assertEqual(inspected, response['properties'])
        self.assertEqual(inspected['frames'], 1_440_000)
        self.assertEqual(inspected['duration_seconds'], 30.0)

        _, again = self.call('/clip', {
            'audio': response['audio'], 'start': 0, 'end': 30, 'gain': 1})
        self.assertEqual(again['properties'], response['properties'])
        self.assert_samples(decode(base64.b64decode(again['audio'])).samples, source)


if __name__ == '__main__':
    unittest.main()
