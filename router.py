import socket
import json
import threading
import time
import os
import ipaddress
import subprocess

# Configuration (to be adjusted per container)
# Environment variables used to facilitate deployment with Docker
MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n for n in os.getenv("NEIGHBORS", "").split(",") if n]
PORT = 5000

VERSION = 1.0
INFINITY = 16
UPDATE_INTERVAL = 5
ROUTE_TIMEOUT = 15
GARBAGE_TIME = 30

# Routing Table: { Subnet: {distance, next_hop, updated_at, is_direct} }
routing_table = {}
table_lock = threading.Lock()
trigger_event = threading.Event()


def get_local_subnets():
	subnets = []
	try:
		output = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
	except Exception:
		return subnets

	for line in output.splitlines():
		parts = line.split()
		if len(parts) < 4:
			continue
		iface = parts[1]
		if iface == "lo":
			continue
		cidr = parts[3]
		try:
			network = ipaddress.ip_interface(cidr).network
		except ValueError:
			continue
		subnets.append(str(network))
	return subnets


def get_iface_for_ip(target_ip):
	try:
		output = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
	except Exception:
		return None

	target = ipaddress.ip_address(target_ip)
	for line in output.splitlines():
		parts = line.split()
		if len(parts) < 4:
			continue
		iface = parts[1]
		if iface == "lo":
			continue
		cidr = parts[3]
		try:
			network = ipaddress.ip_interface(cidr).network
		except ValueError:
			continue
		if target in network:
			return iface
	return None


def get_local_ip_for_neighbor(neighbor_ip):
	try:
		output = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
	except Exception:
		return MY_IP

	target = ipaddress.ip_address(neighbor_ip)
	for line in output.splitlines():
		parts = line.split()
		if len(parts) < 4:
			continue
		if parts[1] == "lo":
			continue
		try:
			interface = ipaddress.ip_interface(parts[3])
		except ValueError:
			continue
		if target in interface.network:
			return str(interface.ip)
	return MY_IP


def route_via_neighbor(subnet, neighbor_ip):
	iface = get_iface_for_ip(neighbor_ip)
	if iface:
		return os.system(f"ip route replace {subnet} via {neighbor_ip} dev {iface} onlink")
	return os.system(f"ip route replace {subnet} via {neighbor_ip} onlink")


def delete_route_via_neighbor(subnet, neighbor_ip):
	iface = get_iface_for_ip(neighbor_ip)
	if iface:
		return os.system(f"ip route del {subnet} via {neighbor_ip} dev {iface}")
	return os.system(f"ip route del {subnet} via {neighbor_ip}")


def init_routing_table():
	now = time.time()
	for subnet in get_local_subnets():
		routing_table[subnet] = {
			"distance": 0,
			"next_hop": "0.0.0.0",
			"updated_at": now,
			"is_direct": True,
		}


def refresh_direct_subnets():
	now = time.time()
	for subnet in get_local_subnets():
		current = routing_table.get(subnet)
		if current is None or not current.get("is_direct"):
			routing_table[subnet] = {
				"distance": 0,
				"next_hop": "0.0.0.0",
				"updated_at": now,
				"is_direct": True,
			}


def build_routes_for_neighbor(neighbor_ip):
	routes = []
	for subnet, info in routing_table.items():
		distance = info["distance"]
		if not info["is_direct"] and info["next_hop"] == neighbor_ip:
			distance = INFINITY
		routes.append({"subnet": subnet, "distance": min(distance, INFINITY)})
	return routes


def broadcast_updates():
	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	while True:
		trigger_event.wait(timeout=UPDATE_INTERVAL)
		trigger_event.clear()

		with table_lock:
			for neighbor in NEIGHBORS:
				sender_ip = get_local_ip_for_neighbor(neighbor)
				packet = {
					"router_id": sender_ip,
					"version": VERSION,
					"routes": build_routes_for_neighbor(neighbor),
				}
				data = json.dumps(packet).encode("utf-8")
				try:
					sock.sendto(data, (neighbor, PORT))
				except OSError:
					continue


def listen_for_updates():
	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	sock.bind(("0.0.0.0", PORT))
	while True:
		data, addr = sock.recvfrom(65535)
		try:
			packet = json.loads(data.decode("utf-8"))
		except (json.JSONDecodeError, UnicodeDecodeError):
			continue

		if packet.get("version") != VERSION:
			continue
		routes = packet.get("routes")
		if not isinstance(routes, list):
			continue

		neighbor_ip = packet.get("router_id") or addr[0]
		update_logic(neighbor_ip, routes)


def update_logic(neighbor_ip, routes_from_neighbor):
	changed = False
	now = time.time()

	with table_lock:
		for route in routes_from_neighbor:
			subnet = route.get("subnet")
			distance = route.get("distance")
			if not subnet or not isinstance(distance, (int, float)):
				continue
			distance = int(distance)
			new_dist = min(distance + 1, INFINITY)
			current = routing_table.get(subnet)

			if current and current.get("is_direct"):
				continue

			if current is None:
				if new_dist < INFINITY:
					routing_table[subnet] = {
						"distance": new_dist,
						"next_hop": neighbor_ip,
						"updated_at": now,
						"is_direct": False,
					}
					route_via_neighbor(subnet, neighbor_ip)
					print(f"[ADD] {subnet} via {neighbor_ip} dist {new_dist}")
					changed = True
				continue

			if current["next_hop"] == neighbor_ip:
				if new_dist >= INFINITY:
					if current["distance"] < INFINITY:
						delete_route_via_neighbor(subnet, neighbor_ip)
					current["distance"] = INFINITY
					current["updated_at"] = now
					print(f"[DOWN] {subnet} via {neighbor_ip} dist {INFINITY}")
					changed = True
				else:
					if new_dist != current["distance"]:
						route_via_neighbor(subnet, neighbor_ip)
						current["distance"] = new_dist
						current["updated_at"] = now
						print(f"[UPD] {subnet} via {neighbor_ip} dist {new_dist}")
						changed = True
					else:
						current["updated_at"] = now
			else:
				if new_dist < current["distance"]:
					routing_table[subnet] = {
						"distance": new_dist,
						"next_hop": neighbor_ip,
						"updated_at": now,
						"is_direct": False,
					}
					route_via_neighbor(subnet, neighbor_ip)
					print(f"[BETTER] {subnet} via {neighbor_ip} dist {new_dist}")
					changed = True

	if changed:
		trigger_event.set()


def route_aging_loop():
	while True:
		now = time.time()
		changed = False
		with table_lock:
			refresh_direct_subnets()
			for subnet, info in list(routing_table.items()):
				if info["is_direct"]:
					continue

				age = now - info["updated_at"]
				if info["distance"] < INFINITY and age > ROUTE_TIMEOUT:
					delete_route_via_neighbor(subnet, info["next_hop"])
					info["distance"] = INFINITY
					info["updated_at"] = now
					print(f"[TIMEOUT] {subnet} via {info['next_hop']}")
					changed = True
					continue

				if info["distance"] >= INFINITY and age > GARBAGE_TIME:
					print(f"[GC] {subnet}")
					del routing_table[subnet]
					changed = True

		if changed:
			trigger_event.set()
		time.sleep(1)


if __name__ == "__main__":
	with table_lock:
		init_routing_table()
	print(f"[START] my_ip={MY_IP} neighbors={NEIGHBORS} direct_subnets={list(routing_table.keys())}")

	threading.Thread(target=broadcast_updates, daemon=True).start()
	threading.Thread(target=route_aging_loop, daemon=True).start()
	listen_for_updates()
