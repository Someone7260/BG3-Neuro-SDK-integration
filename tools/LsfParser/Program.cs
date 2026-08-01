using System;
using LSLib.LS;
class Program {
    static void Main() {
        var param = ResourceLoadParameters.FromGameVersion(LSLib.LS.Enums.Game.BaldursGate3);
        var res = ResourceUtils.LoadResource("d:\\UNI BS\\za career\\contest projects\\BG3 project\\tools\\bg3_extracted\\dialogue\\Mods\\GustavDev\\Story\\DialogsBinary\\Companions\\Gale_InParty2_Nested_AstarionReveal2.lsf", param);
        foreach (var region in res.Regions.Values) Walk(region);
    }
    static void Walk(Node n) {
        if (n.Name == "speaker") {
            Console.WriteLine("SPEAKER:");
            foreach(var attr in n.Attributes) {
                Console.WriteLine("  Attr: " + attr.Key + " = " + attr.Value.Value.ToString());
            }
        }
        foreach (var kv in n.Children) {
            foreach (var child in kv.Value) Walk(child);
        }
    }
}
