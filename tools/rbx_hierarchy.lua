local function json_escape(s)
    return s:gsub('[%z\1-\31\\"]', function(c)
        return string.format("\\u%04x", c:byte())
    end)
end

local function encode_json(v)
    local t = type(v)
    if t == "table" then
        local is_array = (#v > 0)
        local out = {}

        if is_array then
            for i = 1, #v do
                out[#out+1] = encode_json(v[i])
            end
            return "[" .. table.concat(out, ",") .. "]"
        else
            for k, val in pairs(v) do
                out[#out+1] =
                    '"' .. json_escape(k) .. '":' .. encode_json(val)
            end
            return "{" .. table.concat(out, ",") .. "}"
        end
    elseif t == "string" then
        return '"' .. json_escape(v) .. '"'
    elseif t == "number" or t == "boolean" then
        return tostring(v)
    elseif t == "nil" then
        return "null"
    else
        error("unsupported type: " .. t)
    end
end

local path = select(1, ...)
assert(path, "missing path")

local model = fs.read(path, "rbxm")

local nodes = {}
local nextId = 0

local function walk(inst, parent)
    local id = nextId
    nextId = nextId + 1

    nodes[#nodes + 1] = {
        id = id,
        name = inst.Name,
        class = inst.ClassName,
        parent = parent,
    }

    for _, child in ipairs(inst:GetChildren()) do
        walk(child, id)
    end
end

for _, inst in ipairs(model:GetChildren()) do
    walk(inst, -1)
end

print(encode_json(nodes))