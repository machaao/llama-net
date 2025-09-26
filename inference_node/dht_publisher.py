# Import the event-based publisher as the default implementation
from inference_node.event_publisher import EventBasedDHTPublisher

# For backward compatibility, alias the event-based version as DHTPublisher
DHTPublisher = EventBasedDHTPublisher
