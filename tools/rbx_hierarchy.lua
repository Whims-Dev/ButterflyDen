local path = assert(arg[1], "missing rbxm path")

local model = fs.read(path, "rbxm")

local nodes = {}
local nextId = 0

local function walk(inst, parent)
    local id = nextId
    nextId += 1

    table.insert(nodes, {
        id = id,
        name = inst.Name,
        class = inst.ClassName,
        parent = parent,
    })

    for _, child in ipairs(inst:GetChildren()) do
        walk(child, id)
    end
end

for _, inst in ipairs(model:GetChildren()) do
    walk(inst, -1)
end

print(json.encode(nodes))
