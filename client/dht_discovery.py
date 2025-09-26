# Import the event-based discovery as the default implementation
from client.event_discovery import EventBasedDHTDiscovery

# For backward compatibility, alias the event-based version as DHTDiscovery
DHTDiscovery = EventBasedDHTDiscovery
