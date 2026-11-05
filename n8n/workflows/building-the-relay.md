# Building the relay in the editor, once

`imessage-relay.json` is authored in n8n's editor and exported, never written by hand: the editor
is the source of valid node types, versions and parameter shapes. This sheet is what to click.
It only needs doing again if the relay is rebuilt from nothing; a change to the routing goes
through an edit and a fresh export. `tests/test_n8n_workflows.py` checks the result, so a slip here
fails there and is cheap to fix. `../README.md`, "The relay", has the steps before and after this.

Open `http://127.0.0.1:5678`, make a new workflow, and name it exactly `iMessage relay`.

A value marked **expression** goes in with the field switched to *Expression* (the `fx` toggle).
Each expression below is written without the leading `=`, which n8n adds itself.

## The eight nodes

### 1. Webhook, named `Relay webhook`

The trigger: an HTTP POST turns into a run.

| Field | Value |
|---|---|
| HTTP Method | `POST` |
| Path | `imessage-relay` |
| Authentication | `Header Auth` |
| Credential for Header Auth | `Shortcuts relay secret` |
| Respond | `Immediately` |
| Options, IP(s) Allowlist | `127.0.0.1,::1` |

### 2. Edit Fields (Set), named `Routing table`

Holds the table, and keeps the body and nothing else of what came in, so the secret header goes no
further than this node. Mode `Manual Mapping`, with three fields:

| Name | Type | Value |
|---|---|---|
| `zack_handles` | Array | `__ZACK_HANDLES_JSON__`, typed as is. This is the token, so no handle is ever typed here. |
| `received_at` | String | **expression** `{{ $now.toISO() }}` |
| `started_ms` | Number | **expression** `{{ $now.toMillis() }}` |

Then: Include Other Input Fields on; Input Fields to Include `Selected`; Fields to Include `body`.

### 3. Switch, named `Route by sender`

The switchboard. Mode `Rules`, with one routing rule:

- Value 1, **expression**, the whole line:

  ```
  {{ $json.zack_handles.includes(((s) => s.includes('@') ? s.toLowerCase() : ((d) => /^\d{10}$/.test(d) ? '+1' + d : /^1\d{10}$/.test(d) ? '+' + d : d)(s.replace(/[\s().\- ]/g, '')))(String($json.body.sender).trim())) }}
  ```

- Operator: Boolean, `is true`.
- Rename Output on; Output Name `zack`.

Then under Options: Fallback Output `Extra Output`; Rename Fallback Output `dropped`.

The expression is `handles.py`'s `canonical_handle` in one line, so the routing table reads every
written form of a number or an address the way the plugin does.

### 4. HTTP Request, named `Forward to plugin`, from the `zack` output

| Field | Value |
|---|---|
| Method | `POST` |
| URL | `http://127.0.0.1:8645/imessage` |
| Authentication | `Generic Credential Type` |
| Generic Auth Type | `Header Auth` |
| Credential for Header Auth | `Hermes iMessage plugin secret` |
| Send Body | on |
| Body Content Type | `JSON` |
| Specify Body | `Using JSON` |
| JSON | **expression** `{{ $json.body }}` |
| Options, Timeout | `5000` |
| Options, Response | Include Response Headers and Status on; Never Error on |

On the node's **Settings** tab: On Error `Continue (using error output)`; Retry On Fail off.

### 5. Edit Fields (Set), named `Record forward`, from the HTTP Request's success output

| Name | Type | Value |
|---|---|---|
| `route` | String | `zack` |
| `outcome` | String | **expression** `{{ ({202: 'accepted', 204: 'dropped_by_plugin', 401: 'secret_refused'})[$json.statusCode] ?? 'plugin_refusing' }}` |
| `plugin_status` | Number | **expression** `{{ $json.statusCode }}` |
| `received_at` | String | **expression** `{{ $('Routing table').first().json.received_at }}` |
| `duration_ms` | Number | **expression** `{{ $now.toMillis() - $('Routing table').first().json.started_ms }}` |

### 6. Edit Fields (Set), named `Record failure`, from the HTTP Request's error output

| Name | Type | Value |
|---|---|---|
| `route` | String | `zack` |
| `outcome` | String | **expression** `{{ /timeout\|TIMEDOUT/.test(String($json.error?.message) + ' ' + String($json.error?.code)) ? 'timeout' : 'plugin_unreachable' }}` |
| `received_at` | String | **expression** `{{ $('Routing table').first().json.received_at }}` |
| `duration_ms` | Number | **expression** `{{ $now.toMillis() - $('Routing table').first().json.started_ms }}` |

In the `outcome` line, the `\|` is only how this table has to write a `|`. Type a plain `|`. It reads
the error's own two fields and nothing else: n8n puts the whole incoming item beside the error here,
text included, so reading more would let the words of a text decide the outcome.

### 7. Edit Fields (Set), named `Record drop`, from the Switch's `dropped` output

| Name | Type | Value |
|---|---|---|
| `route` | String | `dropped` |
| `outcome` | String | `dropped` |
| `received_at` | String | **expression** `{{ $json.received_at }}` |
| `duration_ms` | Number | **expression** `{{ $now.toMillis() - $json.started_ms }}` |

### 8. Data table, named `Insert run record`, from all three Record nodes

Resource `Row`, Operation `Insert`, the table `run_record` from the list, Columns `Map Each Column
Manually`:

| Column | Value |
|---|---|
| `received_at` | **expression** `{{ $json.received_at }}` |
| `route` | **expression** `{{ $json.route }}` |
| `outcome` | **expression** `{{ $json.outcome }}` |
| `plugin_status` | **expression** `{{ $json.plugin_status }}` |
| `duration_ms` | **expression** `{{ $json.duration_ms }}` |

## The workflow's settings

From the workflow's menu, **Settings**, set each of these explicitly. "Default" fails the test,
since a unit test cannot see the plist that sets the default.

| Setting | Value |
|---|---|
| Save failed production executions | `Do not save` |
| Save successful production executions | `Do not save` |
| Save manual executions | `Do not save` |
| Save execution progress | `Do not save` |

Save the workflow. Do not publish this copy: it holds a token where the handles belong, and it is
deleted once it has been exported.
