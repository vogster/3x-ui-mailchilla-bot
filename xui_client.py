import logging
import threading
import requests
import uuid
from datetime import datetime, timedelta
from email.utils import parseaddr
from urllib.parse import quote, unquote, urlsplit
import config

logger = logging.getLogger(__name__)

# One shared client per process: the session cookies are reused, so the panel
# does not log us in again on every HTTP request the web panel makes.
_shared_client = None
_shared_lock = threading.Lock()


# 3x-ui counts in milliseconds everywhere else, but the last-online map is
# documented in seconds and builds disagree about it. The magnitude tells them
# apart with no guessing at a version: ten digits stays seconds until the year
# 2286, thirteen is already milliseconds.
_SECONDS_CEILING = 10 ** 11


def epoch_ms(value) -> int:
    """A panel timestamp as milliseconds, whichever unit it arrived in. 0 when absent."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return number * 1000 if number < _SECONDS_CEILING else number


def get_shared_client():
    """Returns the process-wide XuiClient instance, created on first use."""
    global _shared_client
    with _shared_lock:
        if _shared_client is None:
            _shared_client = XuiClient()
        return _shared_client


class XuiClient:
    def __init__(self):
        self.base_url = config.XUI_URL
        self.username = config.XUI_USERNAME
        self.password = config.XUI_PASSWORD
        self.api_token = config.XUI_API_TOKEN
        self.session = requests.Session()
        # The instance is shared between threads — the bot and the panel's
        # workers — so requests to 3x-ui are serialised.
        self._lock = threading.RLock()
        if self.api_token:
            self.session.headers.update({"Authorization": f"Bearer {self.api_token}"})
            self.is_logged_in = True
        else:
            self.is_logged_in = False

    def login(self):
        """Signs in to the 3x-ui panel and keeps the session cookies."""
        if self.api_token:
            self.is_logged_in = True
            return True
        url = f"{self.base_url}/login"
        payload = {
            "username": self.username,
            "password": self.password
        }
        try:
            response = self.session.post(url, data=payload, timeout=10)
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("success"):
                    self.is_logged_in = True
                    logger.info("Signed in to 3x-ui.")
                    return True
                else:
                    logger.error(f"Sign-in error from 3x-ui: {resp_json.get('msg')}")
            else:
                logger.error(f"Could not sign in to 3x-ui. Status: {response.status_code}, body: {response.text}")
        except Exception as e:
            logger.error(f"Exception while signing in to 3x-ui: {e}")
        
        self.is_logged_in = False
        return False

    def _request(self, method, endpoint, **kwargs):
        """A wrapper around requests that logs in again when the session expires."""
        with self._lock:
            return self._request_locked(method, endpoint, **kwargs)

    def _request_locked(self, method, endpoint, **kwargs):
        if not self.is_logged_in:
            if not self.login():
                raise Exception("No connection to the 3x-ui API (authorisation failed)")

        url = f"{self.base_url}{endpoint}"
        try:
            response = self.session.request(method, url, timeout=10, **kwargs)
            if response.status_code in (401, 403) or (response.status_code == 200 and "login" in response.url):
                if self.api_token:
                    raise Exception(f"Access denied (status={response.status_code}) with the API token. Check XUI_API_TOKEN.")
                
                logger.info("The 3x-ui session has expired. Signing in again...")
                self.is_logged_in = False
                if self.login():
                    url = f"{self.base_url}{endpoint}"
                    response = self.session.request(method, url, timeout=10, **kwargs)
                else:
                    raise Exception("Signing in to 3x-ui again failed.")
            
            return response
        except requests.RequestException as e:
            logger.error(f"Network error on a request to 3x-ui ({endpoint}): {e}")
            raise

    def add_client(self, email: str, client_uuid: str = None, limit_gb: int = None, expire_days: int = None,
                   inbound_ids: list = None, comment: str = "", flow: str = None,
                   group: str = ""):
        """
        Adds a new client to every inbound listed.
        :param email: the client's unique email/identifier (a limited character
                      set: the panel refuses angle brackets and spaces)
        :param comment: a free-form comment — the sender's name goes here
        :param group: the client group, which this project uses for the tariff
                      name. 3x-ui creates the group on first use, so there is
                      nothing to set up beforehand and this stays one request.
        :param flow: the flow value (taken from the settings when not given)
        :param client_uuid: the client UUID (a new one is generated when absent)
        :param limit_gb: traffic limit in GB (taken from the config when absent)
        :param expire_days: subscription length in days (from the config when absent)
        :param inbound_ids: the inbound ids (taken from the config when absent)
        :return: (client_uuid, list_of_success_inbound_ids), or (None, []) on failure
        """
        if client_uuid is None:
            client_uuid = str(uuid.uuid4())
        
        if limit_gb is None:
            limit_gb = config.LIMIT_GB
            
        if expire_days is None:
            expire_days = config.EXPIRE_DAYS
            
        if inbound_ids is None:
            raw_ids = config.XUI_INBOUND_IDS
            if isinstance(raw_ids, str):
                inbound_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip().isdigit()]
            elif isinstance(raw_ids, list):
                inbound_ids = [int(x) for x in raw_ids]
            else:
                inbound_ids = []

        if not inbound_ids:
            # A client with no inbounds is of no use: the subscription would be
            # empty and the error would surface later, somewhere else. Refuse now.
            logger.error("No inbound is selected — there is nothing to create the client in. "
                         "Tick the inbounds in the panel settings.")
            return None, []

        total_bytes = limit_gb * 1024 * 1024 * 1024 if limit_gb > 0 else 0

        if expire_days > 0:
            expire_time_ms = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)
        else:
            expire_time_ms = 0

        if flow is None:
            flow = config.XUI_FLOW

        payload = {
            "client": {
                "id": client_uuid,
                "uuid": client_uuid,
                "email": email,
                "comment": comment or "",
                "totalGB": total_bytes,
                "expiryTime": expire_time_ms,
                "tgId": 0,
                "subId": "",
                "limitIp": 0,
                "enable": True,
                "flow": flow,
                # Where the client belongs, kept in 3x-ui itself rather than in
                # a file of ours: the panel shows it, an edit made there is the
                # truth, and clients/list hands it back with everything else.
                "group": group or ""
            },
            "inboundIds": inbound_ids
        }
        
        try:
            response = self._request("POST", "/panel/api/clients/add", json=payload)
            
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("success"):
                    logger.info(f"Client {email} added to inbounds {inbound_ids} with UUID {client_uuid}"
                                + (f", group {group!r}" if group else ""))
                    return client_uuid, inbound_ids
                else:
                    logger.error(f"Could not add client {email}: {resp_json.get('msg')}")
            else:
                logger.error(f"Unexpected 3x-ui status while adding a client: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Exception while adding client {email}: {e}")
        
        return None, []
    
    def get_client_data(self, email: str) -> dict or None:
        """
        Fetches a client's full data by email.
        :param email: the client email to look for
        :return: a dict of client data (from 'obj'), or None when not found or on error
        """
        try:
            endpoint = f"/panel/api/clients/get/{quote(str(email), safe='')}"
            response = self._request("GET", endpoint)
            
            if response.status_code == 200:
                resp_json = response.json()
                
                if resp_json.get("success"):
                    return resp_json.get("obj")
                else:
                    logger.error(f"The panel returned an error while fetching data for {email}: {resp_json.get('msg')}")
            else:
                logger.error(f"Unexpected 3x-ui status while fetching data for {email}: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Exception while fetching data for client {email}: {e}")
            
        return None

    def get_all_clients(self) -> list or None:
        """
        Fetches every client from the 3x-ui panel.
        :return: a list of client dicts, or None on error.
        """
        try:
            endpoint = "/panel/api/clients/list"
            response = self._request("GET", endpoint)

            if response.status_code == 200:
                resp_json = response.json()

                if resp_json.get("success"):
                    return resp_json.get("obj", [])
                else:
                    logger.error(f"The panel returned an error while fetching the client list: {resp_json.get('msg')}")
            else:
                logger.error(f"Unexpected 3x-ui status while fetching the client list: {response.status_code}")

        except Exception as e:
            logger.error(f"Exception while fetching the client list: {e}")

        return None

    def get_inbounds(self) -> list:
        """
        Returns the panel's inbounds: id, name (remark), protocol, port, whether
        it is enabled and how many clients it holds.
        The settings page uses this so inbounds can be ticked rather than typed
        in as numeric ids.

        The slim list is enough for that and costs half as much: it carries the
        same inbound rows but strips each client down to a name, and none of
        that is read here. /list would hand over every client's uuid, flow and
        keys on a page that only counts them.
        """
        try:
            response = self._request("GET", "/panel/api/inbounds/list/slim")
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("success"):
                    result = []
                    for ib in resp_json.get("obj") or []:
                        result.append({
                            "id": ib.get("id"),
                            "remark": ib.get("remark") or f"inbound {ib.get('id')}",
                            "protocol": ib.get("protocol") or "",
                            "port": ib.get("port"),
                            "enable": ib.get("enable") is True,
                            "clients": len(ib.get("clientStats") or []),
                        })
                    result.sort(key=lambda i: i["id"] if i["id"] is not None else 0)
                    return result
                logger.error(f"The panel returned an error while fetching the inbound list: {resp_json.get('msg')}")
            else:
                logger.error(f"Unexpected status while fetching the inbound list: {response.status_code}")
        except Exception as e:
            logger.error(f"Exception while fetching the inbound list: {e}")
        return []

    def get_server_status(self) -> dict or None:
        """
        How the machine behind the panel is doing: processor, memory, disk,
        uptime and the versions of the panel and of xray.

        Returns None when the panel does not answer — the dashboard then simply
        leaves the block out rather than showing zeroes that look like a dead
        server.
        """
        try:
            response = self._request("GET", "/panel/api/server/status")
            if response.status_code != 200:
                logger.warning(f"Unexpected status while fetching the server status: {response.status_code}")
                return None
            resp_json = response.json()
            if not resp_json.get("success"):
                logger.warning(f"The panel returned an error for the server status: {resp_json.get('msg')}")
                return None
            obj = resp_json.get("obj") or {}
        except Exception as e:
            logger.warning(f"Exception while fetching the server status: {e}")
            return None

        def pair(section):
            data = obj.get(section) or {}
            used = int(data.get("current") or 0)
            total = int(data.get("total") or 0)
            return {
                "used": used,
                "total": total,
                "percent": round(used / total * 100, 1) if total else 0,
            }

        xray = obj.get("xray") or {}
        loads = obj.get("loads") or []
        # The panel reports its own address here. It is of no use on the
        # dashboard and has a way of ending up in screenshots, so it is dropped.
        return {
            "cpu": round(float(obj.get("cpu") or 0), 1),
            "cpu_cores": obj.get("cpuCores") or 0,
            "loads": [round(float(x), 2) for x in loads[:3]],
            "mem": pair("mem"),
            "swap": pair("swap"),
            "disk": pair("disk"),
            "uptime": int(obj.get("uptime") or 0),
            "connections": int(obj.get("tcpCount") or 0) + int(obj.get("udpCount") or 0),
            "panel_version": str(obj.get("panelVersion") or ""),
            "xray_state": str(xray.get("state") or ""),
            "xray_version": str(xray.get("version") or ""),
            "xray_error": str(xray.get("errorMsg") or ""),
        }

    def get_online_emails(self) -> list or None:
        """
        The clients the panel has seen connected within its heartbeat window.

        Returns None when the panel does not answer or does not know the
        endpoint — an older build has no such route, and a zero there would read
        as "nobody is connected" rather than "this cannot be told".
        """
        try:
            # A POST that changes nothing: the panel simply answers with a list.
            response = self._request("POST", "/panel/api/clients/onlines", json={})
            if response.status_code != 200:
                logger.debug(f"The panel does not report who is online: {response.status_code}")
                return None
            resp_json = response.json()
            if not resp_json.get("success"):
                logger.debug(f"The panel returned an error for the online list: {resp_json.get('msg')}")
                return None
            return [str(x) for x in (resp_json.get("obj") or [])]
        except Exception as e:
            logger.debug(f"Exception while fetching the online list: {e}")
            return None

    def rename_group(self, old_name: str, new_name: str) -> bool:
        """
        Renames a client group, carrying every member across.

        This project names a group after the tariff it stands for, so renaming
        a tariff has to rename the group too — otherwise the clients created
        before the rename stay in a group named after something that no longer
        exists.
        """
        old_name = str(old_name or "").strip()
        new_name = str(new_name or "").strip()
        if not old_name or not new_name or old_name == new_name:
            return True
        try:
            response = self._request("POST", "/panel/api/clients/groups/rename",
                                     json={"oldName": old_name, "newName": new_name})
            if response.status_code != 200:
                logger.error(f"Unexpected status while renaming the group {old_name!r}: "
                             f"{response.status_code}")
                return False
            resp_json = response.json()
            if not resp_json.get("success"):
                # An empty group — a tariff nobody has registered on yet — is
                # not a row in the panel at all, so there is nothing to rename
                # and nothing to worry about.
                logger.info(f"The panel did not rename the group {old_name!r}: "
                            f"{resp_json.get('msg')}. Nobody is in it yet, most likely.")
                return False
            logger.info(f"Group {old_name!r} renamed to {new_name!r}.")
            return True
        except Exception as e:
            logger.error(f"Exception while renaming the group {old_name!r}: {e}")
            return False

    def get_last_online(self) -> dict or None:
        """
        When the panel last saw each client: identifier -> milliseconds.

        None carries the same meaning it does for the online list — the panel
        did not answer, or the build has no such route — and the column then
        stays out of the table altogether. A client missing from the map is a
        different thing: it has simply never connected, which is an answer, and
        the row shows a dash for it.
        """
        try:
            # A POST that changes nothing, the same as the online list.
            response = self._request("POST", "/panel/api/clients/lastOnline", json={})
            if response.status_code != 200:
                logger.debug(f"The panel does not report when clients were last online: "
                             f"{response.status_code}")
                return None
            resp_json = response.json()
            if not resp_json.get("success"):
                logger.debug(f"The panel returned an error for the last-online map: "
                             f"{resp_json.get('msg')}")
                return None
            obj = resp_json.get("obj") or {}
            if not isinstance(obj, dict):
                logger.debug(f"The last-online map came back as {type(obj).__name__}, not a map.")
                return None
            out = {}
            for email, value in obj.items():
                ms = epoch_ms(value)
                if ms:
                    out[str(email)] = ms
            return out
        except Exception as e:
            logger.debug(f"Exception while fetching the last-online map: {e}")
            return None

    def get_client_links(self, email: str) -> list:
        """
        The ready-made connection URLs for one client, one per inbound it is
        attached to — the same strings the panel's own Copy URL button hands out.

        Each URL carries the inbound's name in its fragment, percent-encoded, so
        the label comes from the panel rather than from guesswork here.

        This does not replace the subscription link: that one link keeps working
        as inbounds come and go, and it is what the letters carry. These are for
        the person who wants one particular server by hand.
        """
        try:
            endpoint = f"/panel/api/clients/links/{quote(str(email), safe='')}"
            response = self._request("GET", endpoint)
            if response.status_code != 200:
                logger.warning(f"Unexpected status while fetching the links for {email}: "
                               f"{response.status_code}")
                return []
            resp_json = response.json()
            if not resp_json.get("success"):
                # "record not found" is the ordinary answer for a client the
                # panel does not know, and not worth an error in the log.
                logger.debug(f"The panel returned no links for {email}: {resp_json.get('msg')}")
                return []
            urls = resp_json.get("obj") or []
        except Exception as e:
            logger.warning(f"Exception while fetching the links for {email}: {e}")
            return []

        links = []
        for url in urls:
            url = str(url)
            scheme = url.split("://", 1)[0] if "://" in url else ""
            label = ""
            if "#" in url:
                try:
                    label = unquote(url.rsplit("#", 1)[-1])
                except Exception:
                    label = ""
            # The port is what ties a link back to its inbound; a URL the parser
            # cannot make sense of still keeps its name.
            try:
                port = urlsplit(url).port
            except ValueError:
                port = None
            links.append({"url": url, "protocol": scheme,
                          "label": label or scheme, "port": port})
        return links

    def get_client_traffic(self, email: str):
        """
        Returns the traffic figures for the client given.
        """
        try:
            response = self._request("GET", f"/panel/api/clients/traffic/{quote(str(email), safe='')}")
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("success"):
                    data = resp_json.get("obj")
                    if data:
                        if isinstance(data, list):
                            return data[0] if len(data) > 0 else None
                        return data
                else:
                    logger.warning(f"Could not fetch traffic for {email}: {resp_json.get('msg')}")
            else:
                logger.error(f"Unexpected 3x-ui response while fetching traffic. Status: {response.status_code}")
        except Exception as e:
            logger.error(f"Exception while fetching client traffic from 3x-ui: {e}")

        return None

    # ----------------------------------------------------------------------
    # Finding and managing clients
    # ----------------------------------------------------------------------
    # A 3x-ui client field (which we call "remark" throughout) may hold not only
    # a bare email but also "Name <email>". So looking up by bare email means
    # scanning the client list (find_client_by_email), while updates and deletes
    # are keyed by the client UUID, which is stable.
    # ----------------------------------------------------------------------

    @staticmethod
    def extract_bare_email(remark: str) -> str:
        """
        Pulls the bare email out of a remark shaped as 'Name <email>' or 'email'.
        Returns "" when the remark holds no address: the panel is full of clients
        added by hand whose field carries an arbitrary identifier.
        """
        if not remark:
            return ""
        _, addr = parseaddr(str(remark))
        # parseaddr("user@mail") -> ('user@mail', ''). Fall back to the remark itself.
        candidate = (addr or str(remark)).strip().lower()
        return candidate if "@" in candidate else ""

    def find_client_by_email(self, bare_email: str) -> dict or None:
        """
        Finds a client by bare email, scanning the list of all clients.
        It matches on the email extracted from the remark through parseaddr, or
        on remark == email exactly (older records that carry no name).
        :return: the full client object (an item of get_all_clients), or None.
        """
        if not bare_email:
            return None
        target = bare_email.strip().lower()

        clients = self.get_all_clients()
        if not clients:
            return None

        for client_obj in clients:
            remark = client_obj.get("email")
            if not remark:
                continue
            if str(remark).strip().lower() == target:
                return client_obj
            if self.extract_bare_email(remark) == target:
                return client_obj
        return None

    @staticmethod
    def client_key(client_obj: dict) -> str:
        """
        A stable client identifier for the panel's own links.

        3x-ui hands back two different fields: 'uuid', the client UUID proper,
        and 'id', the record's sequence number as an int. 'uuid' is preferred,
        falling back to 'id' on older or pared-down responses.
        """
        if not client_obj:
            return ""
        return str(client_obj.get("uuid") or client_obj.get("id") or "").strip()

    def find_client_by_uuid(self, client_uuid: str) -> dict or None:
        """
        Finds a client by identifier, scanning the list.
        It matches both 'uuid' and 'id', so links of either shape keep working.
        """
        if not client_uuid:
            return None
        target = str(client_uuid).strip()
        clients = self.get_all_clients()
        if not clients:
            return None
        for client_obj in clients:
            if str(client_obj.get("uuid", "")).strip() == target:
                return client_obj
            if str(client_obj.get("id", "")).strip() == target:
                return client_obj
        return None

    def update_client(self, client_uuid: str, total_gb: int = None,
                      expire_days: int = None, enable: bool = None,
                      new_remark: str = None, new_comment: str = None,
                      group: str = None, client_obj: dict = None) -> bool:
        """
        Updates an existing client.
        It goes GET, then merge, then POST, so that Go zero values do not wipe
        fields out.

        :param client_uuid: the client identifier ('uuid' or 'id')
        :param total_gb: new traffic limit in GB (None leaves it; 0 is unlimited)
        :param expire_days: new length in days from now (None leaves it; 0 never expires)
        :param enable: True/False to switch on or off (None leaves it)
        :param group: the client group — this project's tariff name (None leaves it)
        :param new_remark: a new value for the client's email field (None leaves it)
        :param new_comment: a new comment/name (None leaves it)
        :param client_obj: a client object already fetched — lets the caller avoid
                           asking for the client list a second time
        :return: True on success, False on failure
        """
        if client_obj is None:
            client_obj = self.find_client_by_uuid(client_uuid)
        if not client_obj:
            logger.error(f"Client {client_uuid} was not found to update.")
            return False

        # The current remark goes into the request path (update is keyed by remark/email).
        current_remark = client_obj.get("email", "")

        # Send back EVERY field the panel returned, apart from the nested
        # statistics: clients on non-vless protocols keep privateKey/publicKey/
        # password and such in there, and a pared-down payload would zero them.
        traffic_obj = client_obj.get("traffic") or {}
        payload = {k: v for k, v in client_obj.items() if k != "traffic"}
        payload["up"] = traffic_obj.get("up") or client_obj.get("up") or 0
        payload["down"] = traffic_obj.get("down") or client_obj.get("down") or 0
        payload.setdefault("flow", config.XUI_FLOW)

        # The shape of the /clients/list response and the shape of an update
        # request do not match everywhere, so some fields cannot be copied as they are.
        payload = self._normalize_update_payload(payload, client_obj)

        # Only the fields passed in are overridden
        if new_remark is not None:
            payload["email"] = new_remark
        if new_comment is not None:
            payload["comment"] = new_comment
        if total_gb is not None:
            payload["totalGB"] = total_gb * 1024 * 1024 * 1024 if total_gb > 0 else 0
        if expire_days is not None:
            if expire_days > 0:
                payload["expiryTime"] = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)
            else:
                payload["expiryTime"] = 0
        if enable is not None:
            payload["enable"] = bool(enable)
        if group is not None:
            # Moving a client to another tariff, or out of every group with "".
            # Everything else about the group is the panel's own business: it
            # makes one on first use and forgets an empty one on its own.
            payload["group"] = group

        endpoint = f"/panel/api/clients/update/{quote(str(current_remark), safe='')}"

        try:
            response = self._request("POST", endpoint, json=payload)
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("success"):
                    logger.info(f"Client {current_remark} ({client_uuid}) updated.")
                    return True
                else:
                    logger.error(f"Error updating client {current_remark}: {resp_json.get('msg')}")
            else:
                logger.error(f"Unexpected status while updating client {current_remark}: {response.status_code}")
        except Exception as e:
            logger.error(f"Exception while updating client {current_remark}: {e}")

        return False

    # Fields the panel hands back in one shape and expects in another on update.
    # Each was confirmed by an error from the panel itself, not by guesswork.
    @staticmethod
    def _normalize_update_payload(payload: dict, client_obj: dict) -> dict:
        """
        Reshapes a client object copied out of a response into the form that
        POST /panel/api/clients/update accepts.
        """
        # `id` in the list is the record's number; in a request it is the UUID as a string:
        # "cannot unmarshal number into Go struct field Client.id of type string"
        payload["id"] = str(client_obj.get("uuid") or client_obj.get("id") or "")

        # `allowedIPs` arrives as a string but is expected as an array:
        # "cannot unmarshal string into Go struct field Client.allowedIPs of type []string"
        raw_ips = payload.get("allowedIPs")
        if isinstance(raw_ips, str):
            payload["allowedIPs"] = [p.strip() for p in raw_ips.split(",") if p.strip()]
        elif raw_ips is None:
            payload["allowedIPs"] = []

        # The panel sets the timestamps itself — do not press ours upon it.
        for server_managed in ("createdAt", "updatedAt"):
            payload.pop(server_managed, None)

        return payload

    def delete_client(self, client_uuid: str, keep_traffic: bool = False, client_obj: dict = None):
        """
        Deletes a client.
        :param client_uuid: the client identifier ('uuid' or 'id')
        :param keep_traffic: when True, keeps the traffic figures (?keepTraffic=1)
        :param client_obj: a client object already fetched (saves a list request)
        :return: (True, obj) on success, (False, None) on failure
        """
        if client_obj is None:
            client_obj = self.find_client_by_uuid(client_uuid)
        if not client_obj:
            logger.error(f"Client {client_uuid} was not found to delete.")
            return False, None

        remark = client_obj.get("email", "")
        endpoint = f"/panel/api/clients/del/{quote(str(remark), safe='')}"
        if keep_traffic:
            endpoint += "?keepTraffic=1"

        try:
            response = self._request("POST", endpoint)
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("success"):
                    logger.info(f"Client {remark} ({client_uuid}) deleted.")
                    return True, resp_json.get("obj")
                else:
                    logger.error(f"Error deleting client {remark}: {resp_json.get('msg')}")
            else:
                logger.error(f"Unexpected status while deleting client {remark}: {response.status_code}")
        except Exception as e:
            logger.error(f"Exception while deleting client {remark}: {e}")

        return False, None

    def _attach_or_detach(self, remark: str, action: str, inbound_ids: list) -> bool:
        """Adds a client to inbounds, or takes it out of them, without touching it otherwise."""
        endpoint = f"/panel/api/clients/{quote(str(remark), safe='')}/{action}"
        try:
            response = self._request("POST", endpoint, json={"inboundIds": list(inbound_ids)})
            if response.status_code != 200:
                logger.error(f"Unexpected status on {action} for {remark}: {response.status_code} "
                             f"{response.text[:200]}")
                return False
            resp_json = response.json()
            if resp_json.get("success"):
                return True
            logger.error(f"The panel refused to {action} {remark} "
                         f"{'to' if action == 'attach' else 'from'} {inbound_ids}: {resp_json.get('msg')}")
        except Exception as e:
            logger.error(f"Exception on {action} for {remark}: {e}")
        return False

    def set_client_inbounds(self, client_uuid: str, inbound_ids: list, client_obj: dict = None) -> bool:
        """
        Brings a client's set of inbounds to the one asked for.

        The panel has an operation for each half of it: `attach` puts a client
        into further inbounds, `detach` takes it out of some without deleting
        it. Adding comes first — a failure there leaves the client where it
        already was, whereas detaching first and then failing to attach would
        take access away and not give it back.
        """
        if client_obj is None:
            client_obj = self.find_client_by_uuid(client_uuid)
        if not client_obj:
            logger.error(f"Client {client_uuid} was not found to change its inbounds.")
            return False

        current = [int(i) for i in (client_obj.get("inboundIds") or [])]
        desired = list(dict.fromkeys(int(i) for i in inbound_ids))
        if not desired:
            logger.error("The inbound list is empty — the client would be left without access.")
            return False
        if sorted(current) == sorted(desired):
            logger.info(f"The inbounds of client {client_obj.get('email')} are unchanged.")
            return True

        additions = [i for i in desired if i not in current]
        removals = [i for i in current if i not in desired]
        remark = client_obj.get("email") or ""

        if additions and not self._attach_or_detach(remark, "attach", additions):
            logger.error(f"Client {remark} was not added to inbounds {additions}; "
                         f"nothing else was changed.")
            return False

        if removals and not self._attach_or_detach(remark, "detach", removals):
            logger.error(f"Client {remark} was not removed from inbounds {removals}. "
                         f"{'It was added to ' + str(additions) + ', so it now has more than asked for.' if additions else ''}")
            return False

        logger.info(f"The inbounds of client {remark} are now {desired} "
                    f"(added {additions}, removed {removals}).")
        return True

    def set_client_enabled(self, client_uuid: str, enabled: bool, client_obj: dict = None) -> bool:
        """
        Switches a client on or off through the update endpoint.
        :param client_uuid: the client identifier ('uuid' or 'id')
        :param enabled: True to enable, False to disable
        :param client_obj: a client object already fetched (saves a list request)
        :return: True on success, False on failure
        """
        result = self.update_client(client_uuid, enable=enabled, client_obj=client_obj)
        if enabled:
            done, failed = "enabled", "enable"
        else:
            done, failed = "disabled", "disable"
        if result:
            logger.info(f"Client {client_uuid} {done}.")
        else:
            logger.error(f"Could not {failed} client {client_uuid}.")
        return result