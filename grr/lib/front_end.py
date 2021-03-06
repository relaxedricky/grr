#!/usr/bin/env python
"""The GRR frontend server."""

import operator
import time


import logging

from grr.lib import access_control
from grr.lib import aff4
from grr.lib import communicator
from grr.lib import config_lib
from grr.lib import data_store
from grr.lib import flow
from grr.lib import queue_manager
from grr.lib import rdfvalue
from grr.lib import registry
from grr.lib import stats
from grr.lib import threadpool
from grr.lib import utils
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import flows as rdf_flows


class ServerCommunicator(communicator.Communicator):
  """A communicator which stores certificates using AFF4."""

  def __init__(self, certificate, private_key, token=None):
    self.client_cache = utils.FastStore(1000)
    self.token = token
    super(ServerCommunicator, self).__init__(certificate=certificate,
                                             private_key=private_key)
    self.pub_key_cache = utils.FastStore(max_size=50000)
    # Our common name as an RDFURN.
    self.common_name = rdfvalue.RDFURN(self.certificate.GetCN())

  def _GetRemotePublicKey(self, common_name):
    try:
      # See if we have this client already cached.
      return self.pub_key_cache.Get(str(common_name))
    except KeyError:
      pass

    # Fetch the client's cert and extract the key.
    client = aff4.FACTORY.Create(common_name,
                                 aff4.AFF4Object.classes["VFSGRRClient"],
                                 mode="rw",
                                 token=self.token,
                                 ignore_cache=True)
    cert = client.Get(client.Schema.CERT)
    if not cert:
      stats.STATS.IncrementCounter("grr_unique_clients")
      raise communicator.UnknownClientCert("Cert not found")

    if rdfvalue.RDFURN(cert.GetCN()) != rdfvalue.RDFURN(common_name):
      logging.error("Stored cert mismatch for %s", common_name)
      raise communicator.UnknownClientCert("Stored cert mismatch")

    self.client_cache.Put(common_name, client)
    stats.STATS.SetGaugeValue("grr_frontendserver_client_cache_size",
                              len(self.client_cache))

    pub_key = cert.GetPublicKey()
    self.pub_key_cache.Put(common_name, pub_key)
    return pub_key

  def VerifyMessageSignature(self, response_comms, signed_message_list, cipher,
                             cipher_verified, api_version, remote_public_key):
    """Verifies the message list signature.

    In the server we check that the timestamp is later than the ping timestamp
    stored with the client. This ensures that client responses can not be
    replayed.

    Args:
      response_comms: The raw response_comms rdfvalue.
      signed_message_list: The SignedMessageList rdfvalue from the server.
      cipher: The cipher object that should be used to verify the message.
      cipher_verified: If True, the cipher's signature is not verified again.
      api_version: The api version we should use.
      remote_public_key: The public key of the source.
    Returns:
      An rdf_flows.GrrMessage.AuthorizationState.
    """
    if (not cipher_verified and
        not cipher.VerifyCipherSignature(remote_public_key)):
      stats.STATS.IncrementCounter("grr_unauthenticated_messages")
      return rdf_flows.GrrMessage.AuthorizationState.UNAUTHENTICATED

    try:
      client_id = cipher.cipher_metadata.source
      try:
        client = self.client_cache.Get(client_id)
      except KeyError:
        client = aff4.FACTORY.Create(client_id,
                                     aff4.AFF4Object.classes["VFSGRRClient"],
                                     mode="rw",
                                     token=self.token)
        self.client_cache.Put(client_id, client)
        stats.STATS.SetGaugeValue("grr_frontendserver_client_cache_size",
                                  len(self.client_cache))

      ip = response_comms.orig_request.source_ip
      client.Set(client.Schema.CLIENT_IP(ip))

      # The very first packet we see from the client we do not have its clock
      remote_time = client.Get(client.Schema.CLOCK) or 0
      client_time = signed_message_list.timestamp or 0

      # This used to be a strict check here so absolutely no out of
      # order messages would be accepted ever. Turns out that some
      # proxies can send your request with some delay even if the
      # client has already timed out (and sent another request in
      # the meantime, making the first one out of order). In that
      # case we would just kill the whole flow as a
      # precaution. Given the behavior of those proxies, this seems
      # now excessive and we have changed the replay protection to
      # only trigger on messages that are more than one hour old.

      if client_time < long(remote_time - rdfvalue.Duration("1h")):
        logging.warning("Message desynchronized for %s: %s >= %s", client_id,
                        long(remote_time), int(client_time))
        # This is likely an old message
        return rdf_flows.GrrMessage.AuthorizationState.DESYNCHRONIZED

      stats.STATS.IncrementCounter("grr_authenticated_messages")

      # Update the client and server timestamps only if the client
      # time moves forward.
      if client_time > long(remote_time):
        client.Set(client.Schema.CLOCK, rdfvalue.RDFDatetime(client_time))
        client.Set(client.Schema.PING, rdfvalue.RDFDatetime().Now())
        for label in client.Get(client.Schema.LABELS, []):
          stats.STATS.IncrementCounter("client_pings_by_label",
                                       fields=[label.name])
      else:
        logging.warning("Out of order message for %s: %s >= %s", client_id,
                        long(remote_time), int(client_time))

      client.Flush(sync=False)

    except communicator.UnknownClientCert:
      pass

    return rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED


class FrontEndServer(object):
  """This is the front end server.

  This class interfaces clients into the GRR backend system. We process message
  bundles to and from the client, without caring how message bundles are
  transmitted to the client.

  - receives an encrypted message parcel from clients.
  - Decrypts messages from this.
  - schedules the messages to their relevant queues.
  - Collects the messages from the client queue
  - Bundles and encrypts the messages for the client.
  """

  def __init__(self,
               certificate,
               private_key,
               max_queue_size=50,
               message_expiry_time=120,
               max_retransmission_time=10,
               store=None,
               threadpool_prefix="grr_threadpool"):
    # Identify ourselves as the server.
    self.token = access_control.ACLToken(username="GRRFrontEnd",
                                         reason="Implied.")
    self.token.supervisor = True
    self.throttle_callback = lambda: True
    self.SetThrottleBundlesRatio(None)

    # This object manages our crypto.
    self._communicator = ServerCommunicator(certificate=certificate,
                                            private_key=private_key,
                                            token=self.token)

    self.data_store = store or data_store.DB
    self.receive_thread_pool = {}
    self.message_expiry_time = message_expiry_time
    self.max_retransmission_time = max_retransmission_time
    self.max_queue_size = max_queue_size
    self.thread_pool = threadpool.ThreadPool.Factory(
        threadpool_prefix,
        min_threads=2,
        max_threads=config_lib.CONFIG["Threadpool.size"])
    self.thread_pool.Start()

    # Well known flows are run on the front end.
    self.well_known_flows = (
        flow.WellKnownFlow.GetAllWellKnownFlows(token=self.token))
    well_known_flow_names = self.well_known_flows.keys()
    for well_known_flow in well_known_flow_names:
      if well_known_flow not in config_lib.CONFIG["Frontend.well_known_flows"]:
        del self.well_known_flows[well_known_flow]

  def SetThrottleCallBack(self, callback):
    self.throttle_callback = callback

  def SetThrottleBundlesRatio(self, throttle_bundles_ratio):
    """Sets throttling ration.

    Throttling ratio is a value between 0 and 1 which determines
    which percentage of requests from clients will get proper responses.
    I.e. 0.3 means that only 30% of clients will get new tasks scheduled for
    them when HandleMessageBundles() method is called.

    Args:
      throttle_bundles_ratio: throttling ratio.
    """
    self.throttle_bundles_ratio = throttle_bundles_ratio
    if throttle_bundles_ratio is None:
      self.handled_bundles = []
      self.last_not_throttled_bundle_time = 0

    stats.STATS.SetGaugeValue("grr_frontendserver_throttle_setting",
                              str(throttle_bundles_ratio))

  def UpdateAndCheckIfShouldThrottle(self, bundle_time):
    """Update throttling data and check if request should be throttled.

    When throttling is enabled (self.throttle_bundles_ratio is not None)
    request times are stored. In order to detect whether particular
    request should be throttled, we do the following:
    1. Calculate the average interval between requests over last minute.
    2. Check that [time since last non-throttled request] is less than
       [average interval] / [throttle ratio].

    Args:
      bundle_time: time of the request.

    Returns:
      True if the request should be throttled, False otherwise.
    """
    if self.throttle_bundles_ratio is None:
      return False

    self.handled_bundles.append(bundle_time)
    oldest_limit = bundle_time - config_lib.CONFIG[
        "Frontend.throttle_average_interval"]

    try:
      oldest_index = next(i for i, v in enumerate(self.handled_bundles)
                          if v > oldest_limit)
      self.handled_bundles = self.handled_bundles[oldest_index:]
    except StopIteration:
      self.handled_bundles = []

    blen = len(self.handled_bundles)
    if blen > 1:
      interval = (
          self.handled_bundles[-1] - self.handled_bundles[0]) / float(blen - 1)
    else:
      # TODO(user): this can occasionally return False even when
      # throttle_bundles_ratio is 0, treat it in a generic way.
      return self.throttle_bundles_ratio == 0

    should_throttle = (
        bundle_time - self.last_not_throttled_bundle_time < interval / max(
            0.1e-6, float(self.throttle_bundles_ratio)))

    if not should_throttle:
      self.last_not_throttled_bundle_time = bundle_time

    return should_throttle

  @stats.Counted("grr_frontendserver_handle_num")
  @stats.Timed("grr_frontendserver_handle_time")
  def HandleMessageBundles(self, request_comms, response_comms):
    """Processes a queue of messages as passed from the client.

    We basically dispatch all the GrrMessages in the queue to the task scheduler
    for backend processing. We then retrieve from the TS the messages destined
    for this client.

    Args:
       request_comms: A ClientCommunication rdfvalue with messages sent by the
       client. source should be set to the client CN.

       response_comms: A ClientCommunication rdfvalue of jobs destined to this
       client.

    Returns:
       tuple of (source, message_count) where message_count is the number of
       messages received from the client with common name source.
    """
    messages, source, timestamp = self._communicator.DecodeMessages(
        request_comms)

    now = time.time()
    if messages:
      # Receive messages in line.
      self.ReceiveMessages(source, messages)

    # We send the client a maximum of self.max_queue_size messages
    required_count = max(0, self.max_queue_size - request_comms.queue_size)
    tasks = []

    message_list = rdf_flows.MessageList()
    if self.UpdateAndCheckIfShouldThrottle(time.time()):
      stats.STATS.IncrementCounter("grr_frontendserver_handle_throttled_num")

    elif self.throttle_callback():
      # Only give the client messages if we are able to receive them in a
      # reasonable time.
      if time.time() - now < 10:
        tasks = self.DrainTaskSchedulerQueueForClient(source, required_count)
        message_list.job = tasks

    else:
      stats.STATS.IncrementCounter("grr_frontendserver_handle_throttled_num")

    # Encode the message_list in the response_comms using the same API version
    # the client used.
    try:
      self._communicator.EncodeMessages(message_list,
                                        response_comms,
                                        destination=str(source),
                                        timestamp=timestamp,
                                        api_version=request_comms.api_version)
    except communicator.UnknownClientCert:
      # We can not encode messages to the client yet because we do not have the
      # client certificate - return them to the queue so we can try again later.
      queue_manager.QueueManager(token=self.token).Schedule(tasks)
      raise

    return source, len(messages)

  def DrainTaskSchedulerQueueForClient(self, client, max_count):
    """Drains the client's Task Scheduler queue.

    1) Get all messages in the client queue.
    2) Sort these into a set of session_ids.
    3) Use data_store.DB.ResolvePrefix() to query all requests.
    4) Delete all responses for retransmitted messages (if needed).

    Args:
       client: The ClientURN object specifying this client.

       max_count: The maximum number of messages we will issue for the
                  client.

    Returns:
       The tasks respresenting the messages returned. If we can not send them,
       we can reschedule them for later.
    """
    if max_count <= 0:
      return []

    client = rdf_client.ClientURN(client)

    start_time = time.time()
    # Drain the queue for this client
    new_tasks = queue_manager.QueueManager(token=self.token).QueryAndOwn(
        queue=client.Queue(),
        limit=max_count,
        lease_seconds=self.message_expiry_time)

    initial_ttl = rdf_flows.GrrMessage().task_ttl
    check_before_sending = []
    result = []
    for task in new_tasks:
      if task.task_ttl < initial_ttl - 1:
        # This message has been leased before.
        check_before_sending.append(task)
      else:
        result.append(task)

    if check_before_sending:
      with queue_manager.QueueManager(token=self.token) as manager:
        status_found = manager.MultiCheckStatus(check_before_sending)

        # All messages that don't have a status yet should be sent again.
        for task in check_before_sending:
          if task not in status_found:
            result.append(task)
          else:
            manager.DeQueueClientRequest(client, task.task_id)

    stats.STATS.IncrementCounter("grr_messages_sent", len(result))
    if result:
      logging.debug("Drained %d messages for %s in %s seconds.", len(result),
                    client, time.time() - start_time)

    return result

  def ReceiveMessages(self, client_id, messages):
    """Receives and processes the messages from the source.

    For each message we update the request object, and place the
    response in that request's queue. If the request is complete, we
    send a message to the worker.

    Args:
      client_id: The client which sent the messages.
      messages: A list of GrrMessage RDFValues.
    """
    now = time.time()
    with queue_manager.QueueManager(token=self.token,
                                    store=self.data_store) as manager:
      sessions_handled = []
      for session_id, msgs in utils.GroupBy(
          messages, operator.attrgetter("session_id")).iteritems():

        # Remove and handle messages to WellKnownFlows
        unprocessed_msgs = self.HandleWellKnownFlows(msgs)

        if not unprocessed_msgs:
          continue

        # Keep track of all the flows we handled in this request.
        sessions_handled.append(session_id)

        for msg in unprocessed_msgs:
          manager.QueueResponse(session_id, msg)

        for msg in unprocessed_msgs:
          # Messages for well known flows should notify even though they don't
          # have a status.
          if msg.request_id == 0:
            manager.QueueNotification(session_id=msg.session_id,
                                      priority=msg.priority)
            # Those messages are all the same, one notification is enough.
            break
          elif msg.type == rdf_flows.GrrMessage.Type.STATUS:
            # If we receive a status message from the client it means the client
            # has finished processing this request. We therefore can de-queue it
            # from the client queue.
            manager.DeQueueClientRequest(client_id, msg.task_id)
            manager.QueueNotification(session_id=msg.session_id,
                                      priority=msg.priority,
                                      last_status=msg.request_id)

            stat = rdf_flows.GrrStatus(msg.payload)
            if stat.status == rdf_flows.GrrStatus.ReturnedStatus.CLIENT_KILLED:
              # A client crashed while performing an action, fire an event.
              flow.Events.PublishEvent("ClientCrash",
                                       rdf_flows.GrrMessage(msg),
                                       token=self.token)

    logging.debug("Received %s messages in %s sec", len(messages),
                  time.time() - now)

  def HandleWellKnownFlows(self, messages):
    """Hands off messages to well known flows."""
    msgs_by_wkf = {}
    result = []
    for msg in messages:
      # Regular message - queue it.
      if msg.response_id != 0:
        result.append(msg)
        continue

      # Well known flows:
      flow_name = msg.session_id.FlowName()
      if flow_name in self.well_known_flows:
        # This message should be processed directly on the front end.
        msgs_by_wkf.setdefault(flow_name, []).append(msg)

        # TODO(user): Deprecate in favor of 'well_known_flow_requests'
        # metric.
        stats.STATS.IncrementCounter("grr_well_known_flow_requests")

        stats.STATS.IncrementCounter("well_known_flow_requests",
                                     fields=[str(msg.session_id)])
      else:
        # Message should be queued to be processed in the backend.

        # Well known flows have a response_id==0, but if we queue up the state
        # as that it will overwrite some other message that is queued. So we
        # change it to a random number here.
        msg.response_id = utils.PRNG.GetULong()

        # Queue the message in the data store.
        result.append(msg)

    for flow_name, msg_list in msgs_by_wkf.iteritems():
      wkf = self.well_known_flows[flow_name]
      wkf.ProcessMessages(msg_list)

    return result


class FrontendInit(registry.InitHook):

  def RunOnce(self):
    # Frontend metrics. These metrics should be used by the code that
    # feeds requests into the frontend.
    stats.STATS.RegisterCounterMetric("client_pings_by_label",
                                      fields=[("label", str)])
    stats.STATS.RegisterGaugeMetric("frontend_active_count",
                                    int,
                                    fields=[("source", str)])
    stats.STATS.RegisterGaugeMetric("frontend_max_active_count", int)
    stats.STATS.RegisterCounterMetric("frontend_in_bytes",
                                      fields=[("source", str)])
    stats.STATS.RegisterCounterMetric("frontend_out_bytes",
                                      fields=[("source", str)])
    stats.STATS.RegisterCounterMetric("frontend_request_count",
                                      fields=[("source", str)])
    # Client requests sent to an inactive datacenter. This indicates a
    # misconfiguration.
    stats.STATS.RegisterCounterMetric("frontend_inactive_request_count",
                                      fields=[("source", str)])
    stats.STATS.RegisterEventMetric("frontend_request_latency",
                                    fields=[("source", str)])

    stats.STATS.RegisterEventMetric("grr_frontendserver_handle_time")
    stats.STATS.RegisterCounterMetric("grr_frontendserver_handle_num")
    stats.STATS.RegisterCounterMetric("grr_frontendserver_handle_throttled_num")
    stats.STATS.RegisterGaugeMetric("grr_frontendserver_throttle_setting", str)
    stats.STATS.RegisterGaugeMetric("grr_frontendserver_client_cache_size", int)
    stats.STATS.RegisterCounterMetric("grr_messages_sent")
